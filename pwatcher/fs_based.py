#!/usr/bin/env python2.7
"""Filesytem-based process-watcher.

This is meant to be part of a 2-process system. For now, let's call these processes the Definer and the Watcher.
* The Definer creates a graph of tasks and starts a resolver loop, like pypeflow. It keeps a Waiting list, a Running list, and a Done list. It then communicates with the Watcher.
* The Watcher has 3 basic functions in its API.
  1. Spawn jobs.
  2. Kill jobs.
  3. Query jobs.
1. Spawning jobs
The job definition includes the script, how to run it (locally, qsub, etc.), and maybe some details (unique-id, run-directory). The Watcher then:
  * wraps the script without something to update a heartbeat-file periodically,
  * spawns each job (possibly as a background process locally),
  * and records info (including PID or qsub-name) in a persistent database.
2. Kill jobs.
Since it has a persistent database, it can always kill any job, upon request.
3. Query jobs.
Whenever requested, it can poll the filesystem for all or any jobs, returning the subset of completed jobs. (For NFS efficiency, all the job-exit sentinel files can be in the same directory, along with the heartbeats.)

The Definer would call the Watcher to spawn tasks, and then periodically to poll them. Because these are both now single-threaded, the Watcher *could* be a function within the Definer, or a it could be blocking call to a separate process. With proper locking on the database, users could also query the same executable as a separate process.

Caching/timestamp-checking would be done in the Definer, flexibly specific to each Task.

Eventually, the Watcher could be in a different programming language. Maybe perl. (In bash, a background heartbeat gets is own process group, so it can be hard to clean up.)
"""
try:
    from shlex import quote
except ImportError:
    from pipes import quote
import collections
import contextlib
import glob
import json
import logging
import os
import pprint
import re
import signal
import subprocess
import sys
import time
import traceback

log = logging.getLogger(__name__)

HEARTBEAT_RATE_S = 1 # seconds
ALLOWED_SKEW_S = 30.0 # including the 20s lustre delay
STATE_FN = 'state.py'
Job = collections.namedtuple('Job', ['jobid', 'cmd', 'options'])
MetaJob = collections.namedtuple('MetaJob', ['job', 'lang_exe'])
lang_python_exe = sys.executable
lang_bash_exe = '/bin/bash'

@contextlib.contextmanager
def cd(newdir):
    prevdir = os.getcwd()
    log.debug('CD: %r <- %r' %(newdir, prevdir))
    os.chdir(os.path.expanduser(newdir))
    try:
        yield
    finally:
        log.debug('CD: %r -> %r' %(newdir, prevdir))
        os.chdir(prevdir)

class MetaJobClass(object):
    ext = {
        lang_python_exe: '.py',
        lang_bash_exe: '.bash',
    }
    def get_wrapper(self):
        return 'run-%s%s' %(self.mj.job.jobid, self.ext[self.mj.lang_exe])
    def get_sentinel(self):
        return 'exit-%s' %self.mj.job.jobid # in watched dir
    def get_heartbeat(self):
        return 'heartbeat-%s' %self.mj.job.jobid # in watched dir
    def get_pid(self):
        return self.mj.pid
    def kill(self, pid, sig):
        stored_pid = self.get_pid()
        if not pid:
            pid = stored_pid
            log.info('Not passed a pid to kill. Using stored pid:%s' %pid)
        if pid and stored_pid:
            if pid != stored_pid:
                log.error('pid:%s != stored_pid:%s' %(pid, stored_pid))
        os.kill(pid, sig)
    def __init__(self, mj):
        self.mj = mj
class State(object):
    def get_state_fn(self):
        return os.path.join(self.__directory, STATE_FN)
    def get_directory(self):
        return self.__directory
    def get_directory_wrappers(self):
        return os.path.join(self.__directory, 'wrappers')
    def get_directory_heartbeats(self):
        return os.path.join(self.__directory, 'heartbeats')
    def get_directory_exits(self):
        return os.path.join(self.__directory, 'exits')
    def get_directory_jobs(self):
        # B/c the other directories can get big, we put most per-job data here, under each jobid.
        return os.path.join(self.__directory, 'jobs')
    def get_directory_job(self, jobid):
        return os.path.join(self.get_directory_jobs(), jobid)
    def submit_background(self, bjob):
        """Run job in background.
        Record in state.
        """
        self.top['jobs'][bjob.mjob.job.jobid] = bjob
        jobid = bjob.mjob.job.jobid
        mji = MetaJobClass(bjob.mjob)
        script_fn = os.path.join(self.get_directory_wrappers(), mji.get_wrapper())
        exe = bjob.mjob.lang_exe
        run_dir = self.get_directory_job(jobid)
        makedirs(run_dir)
        with cd(run_dir):
            bjob.submit(self, exe, script_fn) # Can raise
        log.info('Submitted backgroundjob=%s'%repr(bjob))
        self.top['jobids_submitted'].append(jobid)
    def get_mji(self, jobid):
        mjob = self.top['jobs'][jobid].mjob
        return MetaJobClass(mjob)
    def get_bjob(self, jobid):
        return self.top['jobs'][jobid]
    def get_bjobs(self):
        return self.top['jobs']
    def get_mjobs(self):
        return {jobid: bjob.mjob for jobid, bjob in self.top['jobs'].iteritems()}
    def add_deleted_jobid(self, jobid):
        self.top['jobids_deleted'].append(jobid)
    def serialize(state):
        return pprint.pformat(state.top)
    @staticmethod
    def deserialize(directory, content):
        state = State(directory)
        state.top = eval(content)
        state.content_prev = content
        return state
    @staticmethod
    def create(directory):
        state = State(directory)
        makedirs(state.get_directory_wrappers())
        makedirs(state.get_directory_heartbeats())
        makedirs(state.get_directory_exits())
        #system('lfs setstripe -c 1 {}'.format(state.get_directory_heartbeats())) # no improvement noticed
        makedirs(state.get_directory_jobs())
        return state
    def __init__(self, directory):
        self.__directory = os.path.abspath(directory)
        self.content_prev = ''
        self.top = dict()
        self.top['jobs'] = dict()
        self.top['jobids_deleted'] = list()
        self.top['jobids_submitted'] = list()

def get_state(directory):
    state_fn = os.path.join(directory, STATE_FN)
    if not os.path.exists(state_fn):
        return State.create(directory)
    try:
        return State.deserialize(directory, open(state_fn).read())
    except Exception:
        log.exception('Failed to read state "%s". Ignoring (and soon over-writing) current state.'%state_fn)
        # TODO: Backup previous STATE_FN?
        return State(directory)
def State_save(state):
    # TODO: RW Locks, maybe for runtime of whole program.
    content = state.serialize()
    content_prev = state.content_prev
    if content == content_prev:
        return
    fn = state.get_state_fn()
    open(fn, 'w').write(content)
    log.debug('saved state to %s' %repr(os.path.abspath(fn)))
def Job_get_MetaJob(job, lang_exe=lang_bash_exe):
    return MetaJob(job, lang_exe=lang_exe)
def MetaJob_wrap(mjob, state):
    """Write wrapper contents to mjob.wrapper.
    """
    wdir = state.get_directory_wrappers()
    hdir = state.get_directory_heartbeats()
    edir = state.get_directory_exits()
    metajob_rundir = os.getcwd()

    bash_template = """#!%(lang_exe)s
cmd="%(cmd)s"
$cmd
    """
    templates = {
        lang_python_exe: python_template,
        lang_bash_exe: bash_template,
    }
    mji = MetaJobClass(mjob)
    wrapper_fn = os.path.join(wdir, mji.get_wrapper())
    exit_sentinel_fn=os.path.join(edir, mji.get_sentinel())
    heartbeat_fn=os.path.join(hdir, mji.get_heartbeat())
    rate = HEARTBEAT_RATE_S
    command = mjob.job.cmd

    heartbeat_wrapper_template = "heartbeat-wrapper --directory={metajob_rundir} --heartbeat-file={heartbeat_fn} --exit-file={exit_sentinel_fn} --rate={rate} {command}"
    wrapped = heartbeat_wrapper_template.format(**locals())
    log.debug('Wrapped "%s"' %wrapped)

    wrapped = templates[mjob.lang_exe] %dict(
        lang_exe=mjob.lang_exe,
        cmd=wrapped,
    )
    log.debug('Writing wrapper "%s"' %wrapper_fn)
    open(wrapper_fn, 'w').write(wrapped)

def background(script, exe='/bin/bash'):
    """Start script in background (so it keeps going when we exit).
    Run in cwd.
    For now, stdout/stderr are captured.
    Return pid.
    """
    args = [exe, script]
    sin = open(os.devnull)
    sout = open('stdout', 'w')
    serr = open('stderr', 'w')
    pseudo_call = '{exe} {script} 1>|stdout 2>|stderr & '.format(exe=exe, script=script)
    log.debug('dir: {!r}\ncall: {!r}'.format(os.getcwd(), pseudo_call))
    proc = subprocess.Popen([exe, script], stdin=sin, stdout=sout, stderr=serr)
    pid = proc.pid
    log.debug('pid=%s pgid=%s sub-pid=%s' %(os.getpid(), os.getpgid(0), proc.pid))
    checkcall = 'ls -l /proc/{}/cwd'.format(
            proc.pid)
    system(checkcall, checked=True)
    return pid

class MetaJobLocal(object):
    def submit(self, state, exe, script_fn):
        """Can raise.
        """
        sge_option = self.mjob.job.options['sge_option']
        #assert sge_option is None, sge_option # might be set anyway
        pid = background(script_fn, exe=self.mjob.lang_exe)
    def kill(self, state, heartbeat):
        """Can raise.
        (Actually, we could derive heartbeat from state. But for now, we know it anyway.)
        """
        hdir = state.get_directory_heartbeats()
        heartbeat_fn = os.path.join(hdir, heartbeat)
        with open(heartbeat_fn) as ifs:
            line = ifs.readline()
            pid = line.split()[1]
            pid = int(pid)
            pgid = line.split()[2]
            pgid = int(pgid)
            sig =signal.SIGKILL
            log.info('Sending signal(%s) to pgid=-%s (pid=%s) based on heartbeat=%r' %(sig, pgid, pid, heartbeat))
            try:
                os.kill(-pgid, sig)
            except Exception:
                log.exception('Failed to kill(%s) pgid=-%s for %r. Trying pid=%s' %(sig, pgid, heartbeat_fn, pid))
                os.kill(pid, sig)
    def __repr__(self):
        return 'MetaJobLocal(%s)' %repr(self.mjob)
    def __init__(self, mjob):
        self.mjob = mjob # PUBLIC
class MetaJobSge(object):
    def submit(self, state, exe, script_fn):
        """Can raise.
        """
        specific = self.specific
        #cwd = os.getcwd()
        job_name = self.get_jobname()
        sge_option = self.mjob.job.options['sge_option']
        # Add shebang, in case shell_start_mode=unix_behavior.
        #   https://github.com/PacificBiosciences/FALCON/pull/348
        with open(script_fn, 'r') as original: data = original.read()
        with open(script_fn, 'w') as modified: modified.write("#!/bin/bash" + "\n" + data)
        sge_cmd = 'qsub -N {job_name} {sge_option} {specific} -cwd -o stdout -e stderr -S {exe} {script_fn}'.format(
                **locals())
        system(sge_cmd, checked=True) # TODO: Capture q-jobid
    def kill(self, state, heartbeat):
        """Can raise.
        """
        hdir = state.get_directory_heartbeats()
        heartbeat_fn = os.path.join(hdir, heartbeat)
        jobid = self.mjob.job.jobid
        job_name = self.get_jobname()
        sge_cmd = 'qdel {}'.format(
                job_name)
        system(sge_cmd, checked=False)
    def get_jobname(self):
        """Some systems are limited to 15 characters, so for now we simply truncate the jobid.
        TODO: Choose a sequential jobname and record it. Priority: low, since collisions are very unlikely.
        """
        return self.mjob.job.jobid[:15]
    def __repr__(self):
        return 'MetaJobSge(%s)' %repr(self.mjob)
    def __init__(self, mjob):
        self.mjob = mjob
        self.specific = '-V' # pass enV; '-j y' => combine out/err
class MetaJobTorque(MetaJobSge):
    def __repr__(self):
        return 'MetaJobTorque(%s)' %repr(self.mjob)
    def __init__(self, mjob):
        super(MetaJobTorque, self).__init__(mjob)
        self.specific = '-V' # pass enV; '-j oe' => combine out/err
class MetaJobSlurm(object):
    def submit(self, state, exe, script_fn):
        """Can raise.
        """
        specific = self.specific
        job_name = self.get_jobname()
        sge_option = self.mjob.job.options['sge_option']
        cwd = os.getcwd()
        sge_cmd = 'sbatch -J {job_name} {sge_option} {specific} -D {cwd} -o stdout -e stderr -S {exe} {script_fn}'.format(
                **locals())
        system(sge_cmd, checked=True) # TODO: Capture q-jobid
    def kill(self, state, heartbeat):
        """Can raise.
        """
        hdir = state.get_directory_heartbeats()
        heartbeat_fn = os.path.join(hdir, heartbeat)
        jobid = self.mjob.job.jobid
        job_name = self.get_jobname()
        sge_cmd = 'scancel -n {}'.format(
                job_name)
        system(sge_cmd, checked=False)
    def get_jobname(self):
        """Some systems are limited to 15 characters, so for now we simply truncate the jobid.
        TODO: Choose a sequential jobname and record it. Priority: low, since collisions are very unlikely.
        """
        return self.mjob.job.jobid[:15]
    def __repr__(self):
        return 'MetaJobSlurm(%s)' %repr(self.mjob)
    def __init__(self, mjob, sge_option):
        self.mjob = mjob
        self.specific = '-V' # pass enV; '-j y' => combine out/err
class MetaJobLsf(object):
    def submit(self, state, exe, script_fn):
        """Can raise.
        """
        specific = self.specific
        job_name = self.get_jobname()
        sge_option = self.mjob.job.options['sge_option']
        #cwd = os.getcwd() # Always uses CWD.
        sge_cmd = 'bsub -J {job_name} {sge_option} {specific} -o stdout -e stderr "{exe} {script_fn}"'.format(
                **locals())
        system(sge_cmd, checked=True) # TODO: Capture q-jobid
    def kill(self, state, heartbeat):
        """Can raise.
        """
        hdir = state.get_directory_heartbeats()
        heartbeat_fn = os.path.join(hdir, heartbeat)
        jobid = self.mjob.job.jobid
        job_name = self.get_jobname()
        sge_cmd = 'bkill -J {}'.format(
                job_name)
        system(sge_cmd, checked=False)
    def get_jobname(self):
        """Some systems are limited to 15 characters, so for now we simply truncate the jobid.
        TODO: Choose a sequential jobname and record it. Priority: low, since collisions are very unlikely.
        """
        return self.mjob.job.jobid[:15]
    def __repr__(self):
        return 'MetaJobLsf(%s)' %repr(self.mjob)
    def __init__(self, mjob):
        self.mjob = mjob
        self.specific = '-V' # pass enV; '-j y' => combine out/err

def link_rundir(state_rundir, user_rundir):
    if user_rundir:
        link_fn = os.path.join(user_rundir, 'pwatcher.dir')
        if os.path.lexists(link_fn):
            os.unlink(link_fn)
        os.symlink(os.path.abspath(state_rundir), link_fn)

def cmd_run(state, jobids, job_type):
    """On stdin, each line is a unique job-id, followed by run-dir, followed by command+args.
    Wrap them and run them locally, in the background.
    """
    jobs = dict()
    submitted = list()
    result = {'submitted': submitted}
    for jobid, desc in jobids.iteritems():
        assert 'cmd' in desc
        options = {}
        for k in ('sge_option', 'job_type'): # extras to be stored
            if k in desc:
                options[k] = desc[k]
        jobs[jobid] = Job(jobid, desc['cmd'], options)
    log.debug('jobs:\n%s' %pprint.pformat(jobs))
    for jobid, job in jobs.iteritems():
        desc = jobids[jobid]
        log.info('starting job %s' %pprint.pformat(job))
        mjob = Job_get_MetaJob(job)
        MetaJob_wrap(mjob, state)
        options = job.options
        my_job_type = desc.get('job_type')
        if my_job_type is None:
            my_job_type = job_type
        my_job_type = my_job_type.upper()
        if my_job_type == 'LOCAL':
            bjob = MetaJobLocal(mjob)
        elif my_job_type == 'SGE':
            bjob = MetaJobSge(mjob)
        elif my_job_type == 'TORQUE':
            bjob = MetaJobTorque(mjob)
        elif my_job_type == 'SLURM':
            bjob = MetaJobSlurm(mjob)
        elif my_job_type == 'LSF':
            bjob = MetaJobLsf(mjob)
        else:
            raise Exception('Unknown my_job_type=%s' %repr(my_job_type))
        try:
            link_rundir(state.get_directory_job(jobid), desc.get('rundir'))
            state.submit_background(bjob)
            submitted.append(jobid)
        except Exception:
            log.exception('In pwatcher.fs_based.cmd_run(), failed to submit background-job:\n{!r}'.format(
                bjob))
    return result
    # The caller is responsible for deciding what to do about job-submission failures. Re-try, maybe?

re_heartbeat = re.compile(r'heartbeat-(.+)')
def get_jobid_for_heartbeat(heartbeat):
    """This cannot fail unless we change the filename format.
    """
    mo = re_heartbeat.search(heartbeat)
    jobid = mo.group(1)
    return jobid
def system(call, checked=False):
    log.info('!{}'.format(call))
    rc = os.system(call)
    if checked and rc:
        raise Exception('{} <- {!r}'.format(rc, call))
def get_status(state, elistdir, reference_s, sentinel, heartbeat):
    heartbeat_path = os.path.join(state.get_directory_heartbeats(), heartbeat)
    # We take listdir so we can avoid extra system calls.
    if sentinel in elistdir:
        try:
            os.remove(heartbeat_path)
        except Exception:
            log.exception('Unable to remove heartbeat %s when sentinal was found in exit-sentinels listdir.' %repr(heartbeat_path))
        sentinel_path = os.path.join(state.get_directory_exits(), sentinel)
        with open(sentinel_path) as ifs:
            rc = ifs.read().strip()
        return 'EXIT {}'.format(rc)
    # TODO: Record last stat times, to avoid extra stat if too frequent.
    try:
        mtime_s = os.path.getmtime(heartbeat_path)
        if (mtime_s + 3*HEARTBEAT_RATE_S) < reference_s:
            if (ALLOWED_SKEW_S + mtime_s + 3*HEARTBEAT_RATE_S) < reference_s:
                log.debug('DEAD job? {} + 3*{} + {} < {} for {!r}'.format(
                    mtime_s, HEARTBEAT_RATE_S, ALLOWED_SKEW_S, reference_s, heartbeat_path))
                return 'DEAD'
            else:
                log.debug('{} + 3*{} < {} for {!r}. You might have a large clock-skew, or filesystem delays, or just filesystem time-rounding.'.format(
                    mtime_s, HEARTBEAT_RATE_S, reference_s, heartbeat_path))
    except Exception as exc:
        # Probably, somebody deleted it after our call to os.listdir().
        # TODO: Decide what this really means.
        log.debug('Heartbeat not (yet?) found at %r: %r' %(heartbeat_path, exc))
        return 'UNKNOWN'
    return 'RUNNING'
def cmd_query(state, which, jobids):
    """Return the state of named jobids.
    See find_jobids().
    """
    found = dict()
    edir = state.get_directory_exits()
    for heartbeat in find_heartbeats(state, which, jobids):
        jobid = get_jobid_for_heartbeat(heartbeat)
        mji = state.get_mji(jobid)
        sentinel = mji.get_sentinel()
        #system('ls -l {}/{} {}/{}'.format(edir, sentinel, hdir, heartbeat), checked=False)
        found[jobid] = (sentinel, heartbeat)
    elistdir = os.listdir(edir)
    current_time_s = time.time()
    result = dict()
    jobstats = dict()
    result['jobids'] = jobstats
    for jobid, pair in found.iteritems():
        sentinel, heartbeat = pair
        status = get_status(state, elistdir, current_time_s, sentinel, heartbeat)
        log.debug('Status %s for heartbeat:%s' %(status, heartbeat))
        jobstats[jobid] = status
    return result
def get_jobid2pid(pid2mjob):
    result = dict()
    for pid, mjob in pid2mjob.iteritems():
        jobid = mjob.job.jobid
        result[jobid] = pid
    return result
def find_heartbeats(state, which, jobids):
    """Yield heartbeat filenames.
    If which=='list', then query jobs listed as jobids.
    If which=='known', then query all known jobs.
    If which=='infer', then query all jobs with heartbeats.
    These are not quite finished, but already useful.
    """
    #log.debug('find_heartbeats for which=%s, jobids=%s' %(which, pprint.pformat(jobids)))
    if which == 'infer':
        for fn in glob.glob(os.path.join(state.get_directory_heartbeats(), 'heartbeat*')):
            yield fn
    elif which == 'known':
        jobid2mjob = state.get_mjobs()
        for jobid, mjob in jobid2mjob.iteritems():
            mji = MetaJobClass(mjob)
            yield mji.get_heartbeat()
    elif which == 'list':
        jobid2mjob = state.get_mjobs()
        #log.debug('jobid2mjob:\n%s' %pprint.pformat(jobid2mjob))
        for jobid in jobids:
            #log.debug('jobid=%s; jobids=%s' %(repr(jobid), repr(jobids)))
            #if jobid not in jobid2mjob:
            #    log.info("jobid=%s is not known. Might have been deleted already." %jobid)
            mjob = jobid2mjob[jobid]
            mji = MetaJobClass(mjob)
            yield mji.get_heartbeat()
    else:
        raise Exception('which=%s'%repr(which))
def delete_heartbeat(state, heartbeat, keep=False):
    """
    Kill the job with this heartbeat.
    (If there is no heartbeat, then the job is already gone.)
    Delete the entry from state and update its jobid.
    Remove the heartbeat file, unless 'keep'.
    """
    hdir = state.get_directory_heartbeats()
    heartbeat_fn = os.path.join(hdir, heartbeat)
    jobid = get_jobid_for_heartbeat(heartbeat)
    try:
        bjob = state.get_bjob(jobid)
    except Exception:
        log.exception('In delete_heartbeat(), unable to find batchjob for % (from %s)' %(jobid, heartbeat))
        log.warning('Cannot delete. You might be able to delete this yourself if you examine the content of %s.' %heartbeat_fn)
        # TODO: Maybe provide a default grid type, so we can attempt to delete anyway?
        return
    try:
        bjob.kill(state, heartbeat)
    except Exception as exc:
        log.debug('Failed to kill job for heartbeat {!r}: {!r}'.format(
            heartbeat, exc))
    state.add_deleted_jobid(jobid)
    # For now, keep it in the 'jobs' table.
    try:
        os.remove(heartbeat_fn)
        log.debug('Removed heartbeat=%s' %repr(heartbeat))
    except OSError as exc:
        log.debug('Cannot remove heartbeat: {!r}'.format(exc))
    # Note: If sentinel suddenly appeared, that means the job exited. The pwatcher might wrongly think
    # it was deleted, but its output might be available anyway.
def cmd_delete(state, which, jobids):
    """Kill designated jobs, including (hopefully) their
    entire process groups.
    If which=='list', then kill all jobs listed as jobids.
    If which=='known', then kill all known jobs.
    If which=='infer', then kill all jobs with heartbeats.
    Remove those heartbeat files.
    """
    log.debug('Deleting jobs for jobids from %s (%s)' %(
        which, repr(jobids)))
    for heartbeat in find_heartbeats(state, which, jobids):
        delete_heartbeat(state, heartbeat)
def makedirs(path):
    if not os.path.isdir(path):
        os.makedirs(path)
def readjson(ifs):
    """Del keys that start with ~.
    That lets us have trailing commas on all other lines.
    """
    content = ifs.read()
    log.debug('content:%s' %repr(content))
    jsonval = json.loads(content)
    #pprint.pprint(jsonval)
    def striptildes(subd):
        if not isinstance(subd, dict):
            return
        for k,v in subd.items():
            if k.startswith('~'):
                del subd[k]
            else:
                striptildes(v)
    striptildes(jsonval)
    #pprint.pprint(jsonval)
    return jsonval

class ProcessWatcher(object):
    def run(self, jobids, job_type):
        #import traceback; log.debug(''.join(traceback.format_stack()))
        log.debug('run(jobids={}, job_type={})'.format(
            '<%s>'%len(jobids), job_type))
        return cmd_run(self.state, jobids, job_type)
    def query(self, which='list', jobids=[]):
        log.debug('query(which={!r}, jobids={})'.format(
            which, '<%s>'%len(jobids)))
        return cmd_query(self.state, which, jobids)
    def delete(self, which='list', jobids=[]):
        log.debug('delete(which={!r}, jobids={})'.format(
            which, '<%s>'%len(jobids)))
        return cmd_delete(self.state, which, jobids)
    def __init__(self, state):
        self.state = state
@contextlib.contextmanager
def process_watcher(directory):
    """This will (someday) hold a lock, so that
    the State can be written safely at the end.
    """
    state = get_state(directory)
    #log.debug('state =\n%s' %pprint.pformat(state.top))
    yield ProcessWatcher(state)
    # TODO: Sometimes, maybe we should not save state.
    # Or maybe we *should* on exception.
    State_save(state)

def main(prog, cmd, state_dir='mainpwatcher', argsfile=None):
    logging.basicConfig()
    logging.getLogger().setLevel(logging.NOTSET)
    log.warning('logging basically configured')
    log.debug('debug mode on')
    assert cmd in ['run', 'query', 'delete']
    ifs = sys.stdin if not argsfile else open(argsfile)
    argsdict = readjson(ifs)
    log.info('argsdict =\n%s' %pprint.pformat(argsdict))
    with process_watcher(state_dir) as watcher:
        result = getattr(watcher, cmd)(**argsdict)
        if result is not None:
            print pprint.pformat(result)


# With bash, we would need to set the session, rather than
# the process group. That's not ideal, but this is here for reference.
#  http://stackoverflow.com/questions/6549663/how-to-set-process-group-of-a-shell-script
#
bash_template = """#!%(lang_exe)s
cmd='%(cmd)s'
"$cmd"
"""

# perl might be better, for efficiency.
# But we will use python for now.
#
python_template = r"""#!%(lang_exe)s
import threading, time, os, sys

cmd='%(cmd)s'
sentinel_fn='%(sentinel_fn)s'
heartbeat_fn='%(heartbeat_fn)s'
sleep_s=%(sleep_s)s
cwd='%(cwd)s'

os.chdir(cwd)

def log(msg):
    sys.stderr.write(msg)
    sys.stderr.write('\n')
    #sys.stdout.flush()

def thread_heartbeat():
    ofs = open(heartbeat_fn, 'w')
    pid = os.getpid()
    pgid = os.getpgid(0)
    x = 0
    while True:
        ofs.write('{} {} {}\n'.format(x, pid, pgid))
        ofs.flush()
        time.sleep(sleep_s)
        x += 1
def start_heartbeat():
    hb = threading.Thread(target=thread_heartbeat)
    log('alive? {}'.format(hb.is_alive()))
    hb.daemon = True
    hb.start()
    return hb
def main():
    log('cwd:{!r}'.format(os.getcwd()))
    if os.path.exists(sentinel_fn):
        os.remove(sentinel_fn)
    if os.path.exists(heartbeat_fn):
        os.remove(heartbeat_fn)
    os.system('touch {}'.format(heartbeat_fn))
    log("before: pid={}s pgid={}s".format(os.getpid(), os.getpgid(0)))
    try:
        os.setpgid(0, 0)
    except OSError as e:
        log('Unable to set pgid. Possibly a grid job? Hopefully there will be no dangling processes when killed: {}'.format(
            repr(e)))
    log("after: pid={}s pgid={}s".format(os.getpid(), os.getpgid(0)))
    hb = start_heartbeat()
    log('alive? {} pid={} pgid={}'.format(hb.is_alive(), os.getpid(), os.getpgid(0)))
    rc = os.system(cmd)
    # Do not delete the heartbeat here. The discoverer of the sentinel will do that,
    # to avoid a race condition.
    #if os.path.exists(heartbeat_fn):
    #    os.remove(heartbeat_fn)
    with open(sentinel_fn, 'w') as ofs:
        ofs.write(str(rc))
    # sys.exit(rc) # No-one would see this anyway.
    if rc:
        raise Exception('{} <- {!r}'.format(rc, cmd))
main()
"""

if __name__ == "__main__":
    import pdb
    pdb.set_trace()
    main(*sys.argv)

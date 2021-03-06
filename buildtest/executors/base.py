"""
BuildExecutor: manager for test executors
"""
import datetime
import logging
import os
import json
import re
import shutil
import subprocess
import sys
import time

from buildtest.defaults import BUILDTEST_SETTINGS_FILE
from buildtest.utils.file import write_file, read_file
from buildtest.utils.command import BuildTestCommand
from buildtest.utils.timer import Timer


class BuildExecutor:
    """A BuildExecutor is a base class some type of executor, defined under
       the buildtest/settings/default-config.json schema. For example,
       the types "local" and "slurm" would map to `LocalExecutor` and
       `SlurmExecutor` here, each expecting a particular set of
       variables under the config options. If options are required
       and not provided, we exit on error. If they are optional and not
       provided, we use reasonable defaults.
    """

    def __init__(self, config_opts):
        """initiate executors, meaning that we provide the config_opts
           that are validated, and can instantiate each executor to be available

           Parameters:

           :param config_opts: the validated config opts provided by buildtest.
           :type config_opts: dictionary, required
        """

        self.executors = {}
        self.logger = logging.getLogger(__name__)
        self.logger.debug("Getting Executors from buildtest settings")

        for name in config_opts["executors"].get("local", {}).keys():
            self.executors[f"local.{name}"] = LocalExecutor(
                name, config_opts["executors"]["local"][name], config_opts
            )

        for name in config_opts["executors"].get("ssh", {}).keys():
            self.executors[f"ssh.{name}"] = SSHExecutor(
                name, config_opts["executors"]["ssh"][name], config_opts
            )

        for name in config_opts["executors"].get("slurm", {}).keys():
            self.executors[f"slurm.{name}"] = SlurmExecutor(
                name, config_opts["executors"]["slurm"][name], config_opts
            )

        for name in config_opts["executors"].get("lsf", {}).keys():
            self.executors[f"lsf.{name}"] = LSFExecutor(
                name, config_opts["executors"]["lsf"][name], config_opts
            )

    def __str__(self):
        return "[buildtest-executor]"

    def __repr__(self):
        return "[buildtest-executor]"

    def get(self, name):
        """Given the name of an executor return the executor for running 
           a buildtest build, or get the default.
        """
        return self.executors.get(name)

    def _choose_executor(self, builder):
        """Choose executor is called at the onset of a run or dryrun. We
           look at the builder metadata to determine if a default
           is set for the executor, and fall back to the default.

           Parameters:

           :param builder: the builder with the loaded Buildspec.
           :type builder: buildtest.buildsystem.BuilderBase (or subclass).
        """

        executor = builder.metadata.get("recipe").get("executor")
        # if executor not defined in buildspec we raise an error
        if not executor:
            msg = "[%s]: 'executor' key not defined in buildspec: %s" % (
                builder.metadata["name"],
                builder.metadata["buildspec"],
            )
            builder.logger.error(msg)
            builder.logger.debug("test: %s", builder.metadata["recipe"])
            sys.exit(msg)

        # The executor (or a default) must be define
        if executor not in self.executors:
            msg = "[%s]: executor %s is not defined in %s" % (
                builder.metadata["name"],
                executor,
                BUILDTEST_SETTINGS_FILE,
            )
            builder.logger.error(msg)
            sys.exit(msg)

        # Get the executor by name, and add the builder to it
        executor = self.executors.get(executor)
        executor.builder = builder
        return executor

    def run(self, builder):
        """Given a buildtest.buildsystem.BuildspecParser (subclass) go through the
           steps defined for the executor to run the build. This should
           be instantiated by the subclass. For a simple script run, we expect a 
           setup, build, and finish.

           :param builder: the builder with the loaded test configuration.
           :type builder: buildtest.buildsystem.BuilderBase (or subclass).
        """
        executor = self._choose_executor(builder)

        if executor.type == "local":
            executor.run()
        elif executor.type in ["slurm", "lsf"]:
            executor.dispatch()

        return executor.result

    def poll(self, builder):

        executor = self._choose_executor(builder)
        if executor.type == "type":
            return True

        # poll slurm job
        if executor.type == "slurm":
            # only poll job if its in PENDING or RUNNING state
            if executor.job_state in ["PENDING", "RUNNING"] or not executor.job_state:
                executor.poll()
            else:
                executor.gather()
                return True

        elif executor.type == "lsf":
            # only poll job if its in PENDING or RUNNING state
            if executor.job_state in ["PEND", "RUN"] or not executor.job_state:
                executor.poll()
            else:
                executor.gather()
                return True

        return False


class BaseExecutor:
    """The BaseExecutor is an abstract base class for all executors. All
       executors must have a listing of steps and dryrun_steps
    """

    steps = ["setup", "run"]
    type = "base"

    def __init__(self, name, settings, config_opts):
        """Initiate a base executor, meaning we provide a name (also held
           by the BuildExecutor base that holds it) and the loaded dictionary
           of config opts to parse.

            Parameters:

           :param name: a name for the base executor and key provided in the configuration file
           :type name: string (required)
           :param settings: the original config opts to extract variables from.
           :type settings: dict (required)
           :param builder: the builder object for the executor to control.
           :type builder: buildtest.buildsystem.base.BuilderBase (or subclass).
        """
        self.logger = logging.getLogger(__name__)
        self.name = name
        self._settings = settings
        self._buildtestsettings = config_opts
        self.load()
        self.builder = None
        self.result = {}

    def load(self):
        """Load a particular configuration based on the name. This method
           should set defaults for the executor, and will vary based on the
           class.
        """
        pass

    def run(self):
        """The run step basically runs the build. This is run after setup
           so we are sure that the builder is defined. This is also where
           we set the result to return.
        """
        pass

    def __str__(self):
        return "%s.%s" % (self.type, self.name)

    def __repr__(self):
        return self.__str__()

    def get_formatted_time(self, key, fmt="%Y/%m/%d %X"):
        """Given some timestamp key in self.metadata, return a pretty printed
           version of it. This is intended to log in the console for the user.

           Parameters:

           key: The key to look up in the metadata
           fmt: the format string to use
        """
        timestamp = self.builder.metadata.get(key, "")
        if timestamp:
            timestamp = timestamp.strftime(fmt)
        return timestamp

    def check_regex(self, regex):
        """ This method conducts a regular expression check using 're.search' with regular
            expression defined in Buildspec. User must specify an output stream (stdout, stderr)
            to select when performing regex. In buildtest, this would read the .out or .err file
            based on stream and run the regular expression to see if there is a match.

            Parameters:

            :param regex: Regular expression object defined in Buildspec file
            :type regex: str, required
            :return:  A boolean return True/False based on if re.search is successful or not
            :rtype: bool
        """

        if regex["stream"] == "stdout":
            self.logger.debug(
                f"Detected regex stream 'stdout' so reading output file: {self.builder.metadata['outfile']}"
            )
            content = read_file(self.builder.metadata["outfile"])

        elif regex["stream"] == "stderr":
            self.logger.debug(
                f"Detected regex stream 'stderr' so reading error file: {self.builder.metadata['errfile']}"
            )
            content = read_file(self.builder.metadata["errfile"])

        self.logger.debug(f"Applying re.search with exp: {regex['exp']}")

        # perform a regex search based on value of 'exp' key defined in Buildspec with content file (output or error)
        return re.search(regex["exp"], content) != None

    def write_testresults(self, out, err):
        """This method writes test results into output and error file.

           Parameters
           :param out: content of output stream
           :type out: list
           :param err: content of error stream
           :type err: list
        """

        # Keep an output file
        run_output_file = os.path.join(
            self.builder.metadata.get("rundir"), self.builder.metadata.get("build_id")
        )
        outfile = run_output_file + ".out"
        errfile = run_output_file + ".err"

        # write output of test to .out file
        out = "".join(out)
        err = "".join(err)

        self.logger.debug(f"Writing run output to file: {outfile}")
        write_file(outfile, out)

        # write error from test to .err file
        self.logger.debug(f"Writing run error to file: {errfile}")
        write_file(errfile, err)

        self.builder.metadata["outfile"] = outfile
        self.builder.metadata["errfile"] = errfile

    def check_test_state(self):
        """This method is responsible for detecting state of test (PASS/FAIL)
           based on returncode or regular expression.
        """
        status = self.builder.recipe.get("status")

        self.result["state"] = "FAIL"
        # if status is defined in Buildspec, then check for returncode and regex
        if status:

            # returncode_match is boolean to check if reference returncode matches return code from test
            returncode_match = False

            # regex_match is boolean to check if output/error stream matches regex defined in Buildspec,
            # if no regex is defined we set this to True since we do a logical AND
            regex_match = False

            slurm_job_state_match = False
            if status.get("returncode"):
                self.logger.debug("Conducting Return Code check")
                self.logger.debug(
                    "Status Return Code: %s   Result Return Code: %s"
                    % (status["returncode"], self.result["returncode"])
                )
                # checks if test returncode matches returncode specified in Buildspec and assign boolean to returncode_match
                returncode_match = status["returncode"] == self.result["returncode"]

            if status.get("regex"):
                self.logger.debug("Conducting Regular Expression check")
                # self.check_regex  applies regular expression check specified in Buildspec with output or error
                # stream. self.check_regex returns a boolean (True/False) by using re.search
                regex_match = self.check_regex(status["regex"])

            # if slurm_job_state_codes defined in buildspec.
            # self.builder.metadata["job"] only defined when job run through SlurmExecutor
            if status.get("slurm_job_state_codes") and self.builder.metadata.get("job"):
                slurm_job_state_match = (
                    status["slurm_job_state_codes"]
                    == self.builder.metadata["job"]["State"]
                )

            self.logger.info(
                "ReturnCode Match: %s Regex Match: %s Slurm Job State Match: %s"
                % (returncode_match, regex_match, slurm_job_state_match)
            )

            if returncode_match or regex_match or slurm_job_state_match:
                self.result["state"] = "PASS"

        # if status is not defined we check test returncode, by default 0 is PASS and any other return code is a FAIL
        else:
            if self.result["returncode"] == 0:
                self.result["state"] = "PASS"

        # Return to starting directory for next test
        os.chdir(self.builder.pwd)

        self.builder.metadata["result"] = self.result


class LocalExecutor(BaseExecutor):
    type = "local"

    def load(self):
        self.shell = self._settings.get("shell")

        self.check()

    def check(self):

        if not shutil.which(self.shell):
            sys.exit(f"Unable to find shell: {self.shell}")

    def run(self):
        """This method is responsible for running test for LocalExecutor which
           runs test locally. We keep track of metadata in ``self.builder.metadata``
           and self.result keeps track of run result. The output and error file
           is written to filesystem. After test
        """
        # Keep a result object
        # self.result = {}

        # check shell type mismatch between buildspec shell and executor shell. We can't support python with sh/bash.
        if (
            self.builder.shell.name in ["sh", "bash", "/bin/bash", "/bin/sh"]
            and self.shell == "python"
        ) or (
            self.builder.shell.name == "python"
            and self.shell in ["sh", "bash", "/bin/bash", "/bin/sh"]
        ):
            sys.exit(
                f"[{self.name}]: shell mismatch, expecting {self.shell} while buildspec shell is {self.builder.shell.name}"
            )

        self.result["LOGFILE"] = self.builder.metadata.get("logfile", "")
        self.result["BUILD_ID"] = self.builder.metadata.get("build_id")

        # Change to the test directory
        os.chdir(self.builder.metadata["testroot"])
        self.logger.debug(f"Changing to directory {self.builder.metadata['testroot']}")

        # build the run command that includes the shell path, shell options and path to test file
        cmd = [
            self.builder.shell.path,
            self.builder.shell.opts,
            self.builder.metadata["testpath"],
        ]
        self.builder.metadata["command"] = " ".join(cmd)
        self.logger.debug(
            f"Running Test via command: {self.builder.metadata['command']}"
        )

        command = BuildTestCommand(self.builder.metadata["command"])
        self.builder.metadata["starttime"] = datetime.datetime.now()
        self.result["starttime"] = self.get_formatted_time("starttime")

        t = Timer()
        t.start()
        out, err = command.execute()
        self.result["runtime"] = t.stop()

        self.result["endtime"] = self.get_formatted_time("endtime")

        self.write_testresults(out, err)

        self.logger.debug(
            f"Return code: {command.returncode} for test: {self.builder.metadata['testpath']}"
        )
        self.result["returncode"] = command.returncode

        self.write_testresults(out, err)
        self.check_test_state()


class SSHExecutor(BaseExecutor):
    type = "ssh"


class SlurmExecutor(BaseExecutor):
    """The SlurmExecutor class is responsible for submitting jobs to Slurm Scheduler.
       The SlurmExecutor performs the following steps

       check: check if slurm partition is available for accepting jobs.
       load: load slurm configuration from buildtest configuration file
       dispatch: dispatch job to scheduler and acquire job ID
       poll: wait for Slurm jobs to finish
       gather: Once job is complete, gather job data
    """

    type = "slurm"
    steps = ["dispatch", "poll", "gather", "close"]
    job_state = None
    poll_cmd = "sacct"
    sacct_fields = [
        "Account",
        "AllocNodes",
        "AllocTRES",
        "ConsumedEnergyRaw",
        "CPUTimeRaw",
        "End",
        "ExitCode",
        "JobID",
        "JobName",
        "NCPUS",
        "NNodes",
        "QOS",
        "ReqGRES",
        "ReqMem",
        "ReqNodes",
        "ReqTRES",
        "Start",
        "State",
        "Submit",
        "UID",
        "User",
        "WorkDir",
    ]

    def check(self):
        """Check slurm binary is available before running tests. This will check
           the launcher (sbatch) and sacct are available. If qos, partition, and
           cluster key defined we check if its a valid entity in slurm configuration.
           For partition, we also check if its in the ``up`` state before dispatching
           jobs. This method will raise an exception of type SystemExit if any
           checks fail.
        """

        if not shutil.which(self.launcher):
            sys.exit(
                f"[{self.builder.metadata['name']}]: Cannot find launcher program: {self.launcher}"
            )

        if not shutil.which(self.poll_cmd):
            sys.exit(
                f"[{self.builder.metadata['name']}]: Cannot find slurm poll command: {self.poll_cmd}"
            )

    def load(self):
        """Load the a slurm executor configuration from buildtest settings."""

        self.launcher = self._settings.get("launcher") or self._buildtestsettings[
            "executors"
        ].get("defaults", {}).get("launcher")
        self.launcher_opts = self._settings.get("options")

        self.cluster = self._settings.get("cluster")
        self.partition = self._settings.get("partition")
        self.qos = self._settings.get("qos")

    def dispatch(self):
        """This method is responsible for dispatching job to slurm scheduler."""

        self.check()

        self.result["BUILD_ID"] = self.builder.metadata.get("build_id")

        os.chdir(self.builder.metadata["testroot"])
        self.logger.debug(f"Changing to directory {self.builder.metadata['testroot']}")

        sbatch_cmd = [self.launcher]

        if self.partition:
            sbatch_cmd += [f"-p {self.partition}"]

        if self.qos:
            sbatch_cmd += [f"-q {self.qos}"]

        if self.cluster:
            sbatch_cmd += [f"-M {self.cluster}"]

        if self.launcher_opts:
            sbatch_cmd += [" ".join(self.launcher_opts)]

        sbatch_cmd.append(self.builder.metadata["testpath"])

        self.builder.metadata["command"] = " ".join(sbatch_cmd)
        self.logger.debug(
            f"Running Test via command: {self.builder.metadata['command']}"
        )

        self.builder.metadata["starttime"] = datetime.datetime.now()
        self.result["starttime"] = self.get_formatted_time("starttime")

        command = BuildTestCommand(self.builder.metadata["command"])
        command.execute()
        out = command.get_output()
        err = command.get_error()

        # if sbatch job submission returns non-zero exit that means we have failure, exit immediately
        if command.returncode != 0:
            err = f"[{self.builder.metadata['name']}] failed to submit job with returncode: {command.returncode} \n"
            err += f"[{self.builder.metadata['name']}] running command: {sbatch_cmd}"
            sys.exit(err)

        interval = 2

        print(f"[{self.builder.metadata['name']}] job dispatched to scheduler")
        print(
            f"[{self.builder.metadata['name']}] acquiring job id in {interval} seconds"
        )

        # wait 10 seconds before querying slurm for jobID. It can take some time for output
        # of job to show up from time of submission and running squeue.
        time.sleep(interval)

        cmd = ["sacct"]
        if self.cluster:
            cmd += [f"-M {self.cluster}"]
        cmd += ["-X -n -P -u $USER --format=job | tail -n 1"]
        cmd = " ".join(cmd)

        # get last job ID
        self.logger.debug(f"[Acquire Job ID]: {cmd}")
        output = subprocess.check_output(cmd, shell=True, universal_newlines=True)
        self.job_id = int(output.strip())
        self.logger.debug(
            f"[{self.builder.metadata['name']}] JobID: {self.job_id} dispatched to scheduler"
        )
        self.result["state"] = "N/A"
        self.result["runtime"] = "0"
        self.result["returncode"] = "0"
        self.write_testresults(out, err)

    def poll(self):
        """ This method will poll for job each interval specified by time interval
            until job finishes. We use `sacct` to poll for job id and sleep for given
            time interval until trying again. The command to be run is
            ``sacct -j <jobid> -o State -n -X -P``
        """

        self.logger.debug(f"Query Job: {self.job_id}")

        slurm_query = f"{self.poll_cmd} -j {self.job_id} -o State -n -X -P"

        # to query jobs from another cluster we must add -M <cluster> to sacct
        if self.cluster:
            slurm_query += f" -M {self.cluster}"

        self.logger.debug(slurm_query)
        cmd = BuildTestCommand(slurm_query)
        cmd.execute()
        self.job_state = cmd.get_output()
        self.job_state = "".join(self.job_state).rstrip()
        msg = f"[{self.builder.metadata['name']}]: JobID {self.job_id} in {self.job_state} state "
        print(msg)
        self.logger.debug(msg)
        return self.job_state

    def gather(self):
        """Gather Slurm detail after job completion"""

        gather_cmd = f"{self.poll_cmd} -j {self.job_id} -X -n -P -o {','.join(self.sacct_fields)}"

        # to query jobs from another cluster we must add -M <cluster> to sacct
        if self.cluster:
            gather_cmd += f" -M {self.cluster}"

        self.logger.debug(f"Gather slurm job data by running: {gather_cmd}")
        cmd = BuildTestCommand(gather_cmd)
        cmd.execute()
        out = "".join(cmd.get_output())
        # split by | since
        out = out.split("|")
        job_data = {}

        self.logger.debug(f"[{self.builder.name}] Job Results:")
        for field, value in zip(self.sacct_fields, out):
            job_data[field] = value
            self.logger.debug(f"field: {field}   value: {value}")

        self.builder.metadata["job"] = job_data

        # Exit Code field is in format <ExitCode>:<Signal> for now we care only
        # about first number
        self.result["returncode"] = int(job_data["ExitCode"].split(":")[0])

        self.result["endtime"] = job_data["End"]
        self.builder.metadata["outfile"] = os.path.join(
            job_data["WorkDir"].rstrip(), f"slurm-{job_data['JobID']}.out"
        )
        self.builder.metadata["errfile"] = os.path.join(
            job_data["WorkDir"].rstrip(), f"slurm-{job_data['JobID']}.err"
        )
        self.logger.debug(f"[{self.builder.name}] result: {self.result}")
        self.logger.debug(
            f"[{self.builder.name}] returncode: {self.result['returncode']}"
        )
        self.check_test_state()


class LSFExecutor(BaseExecutor):
    """The LSFExecutor class is responsible for submitting jobs to LSF Scheduler.
       The LSFExecutor performs the following steps

       check: check if lsf queue is available for accepting jobs.
       load: load lsf configuration from buildtest configuration file
       dispatch: dispatch job to scheduler and acquire job ID
       poll: wait for LSF jobs to finish
       gather: Once job is complete, gather job data
    """

    type = "lsf"
    steps = ["check", "dispatch", "poll", "gather", "close"]
    job_state = None
    poll_cmd = "bjobs"
    # format fields we retrieve in gather step
    format_fields = [
        "job_name",
        "stat",
        "user",
        "user_group",
        "queue",
        "proj_name",
        "pids",
        "exit_code",
        "from_host",
        "exec_host",
        "submit_time",
        "start_time",
        "finish_time",
        "nthreads",
        "exec_home",
        "exec_cwd",
        "output_file",
        "error_file",
    ]

    def check(self):
        """Checking binary for lsf launcher and poll command. If not found we raise error"""
        if not shutil.which(self.launcher):
            sys.exit(
                f"[{self.builder.metadata['name']}]: Cannot find launcher program: {self.launcher}"
            )

        if not shutil.which(self.poll_cmd):
            sys.exit(
                f"[{self.builder.metadata['name']}]: Cannot find poll command: {self.poll_cmd}"
            )

    def load(self):
        """Load the a LSF executor configuration from buildtest settings."""

        self.launcher = self._settings.get("launcher") or self._buildtestsettings[
            "executors"
        ].get("defaults", {}).get("launcher")
        self.launcher_opts = self._settings.get("options")

        self.queue = self._settings.get("queue")

    def dispatch(self):
        """This method is responsible for dispatching job to slurm scheduler."""

        self.check()

        self.result["BUILD_ID"] = self.builder.metadata.get("build_id")

        os.chdir(self.builder.metadata["testroot"])
        self.logger.debug(f"Changing to directory {self.builder.metadata['testroot']}")

        bsub_cmd = [self.launcher]

        if self.queue:
            bsub_cmd += [f"-q {self.queue}"]

        if self.launcher_opts:
            bsub_cmd += [" ".join(self.launcher_opts)]

        bsub_cmd.append(self.builder.metadata["testpath"])

        self.builder.metadata["command"] = " ".join(bsub_cmd)
        self.logger.debug(
            f"Running Test via command: {self.builder.metadata['command']}"
        )

        self.builder.metadata["starttime"] = datetime.datetime.now()
        self.result["starttime"] = self.get_formatted_time("starttime")

        command = BuildTestCommand(self.builder.metadata["command"])
        command.execute()
        out = command.get_output()
        err = command.get_error()

        # if sbatch job submission returns non-zero exit that means we have failure, exit immediately
        if command.returncode != 0:
            err = f"[{self.builder.metadata['name']}] failed to submit job with returncode: {command.returncode} \n"
            err += f"[{self.builder.metadata['name']}] running command: {bsub_cmd}"
            sys.exit(err)

        interval = 5

        print(f"[{self.builder.metadata['name']}] job dispatched to scheduler")
        print(
            f"[{self.builder.metadata['name']}] acquiring job id in {interval} seconds"
        )

        # wait 10 seconds before querying slurm for jobID. It can take some time for output
        # of job to show up from time of submission and running squeue.
        time.sleep(interval)

        cmd = ["bjobs"]

        cmd += ["-u $USER -o 'JobID' -noheader | tail -n 1"]
        cmd = " ".join(cmd)

        # get last job ID
        self.logger.debug(f"[Acquire Job ID]: {cmd}")
        output = subprocess.check_output(cmd, shell=True, universal_newlines=True)
        self.job_id = int(output.strip())
        self.logger.debug(
            f"[{self.builder.metadata['name']}] JobID: {self.job_id} dispatched to scheduler"
        )
        self.result["state"] = "N/A"
        self.result["runtime"] = "0"
        self.result["returncode"] = "0"
        self.write_testresults(out, err)

    def poll(self):
        """ This method will poll for job by using bjobs and return state of job.
            The command to be run is ``bjobs -noheader -o 'stat' <JOBID>``
             which returns job state.
        """

        self.logger.debug(f"Query Job: {self.job_id}")

        query = f"{self.poll_cmd} -noheader -o 'stat' {self.job_id}"

        self.logger.debug(query)
        cmd = BuildTestCommand(query)
        cmd.execute()
        self.job_state = cmd.get_output()
        self.job_state = "".join(self.job_state).rstrip()
        msg = f"[{self.builder.metadata['name']}]: JobID {self.job_id} in {self.job_state} state "
        print(msg)
        self.logger.debug(msg)
        return self.job_state

    def gather(self):
        """Gather Job detail after completion of job. This method will retrieve output
           fields defined for ``self.format_fields``. buildtest will run
           ``bjobs -o '<field1> ... <fieldN>' <JOBID> -json``.
        """
        # command
        gather_cmd = (
            f"{self.poll_cmd} -o '{' '.join(self.format_fields)}' {self.job_id} -json"
        )

        self.logger.debug(f"Gather LSF job data by running: {gather_cmd}")
        cmd = BuildTestCommand(gather_cmd)
        cmd.execute()
        out = cmd.get_output()
        out = "".join(out).rstrip()

        out = json.loads(out)
        print(json.dumps(out, indent=2))

        job_data = {}

        self.logger.debug(f"[{self.builder.name}] Job Results:")
        records = out["RECORDS"][0]
        for field, value in records.items():
            job_data[field] = value
            self.logger.debug(f"field: {field}   value: {value}")

        self.builder.metadata["job"] = job_data

        # Exit Code field is in format <ExitCode>:<Signal> for now we care only
        # about first number
        if job_data["EXIT_CODE"] == "":
            self.result["returncode"] = 0
        else:
            self.result["returncode"] = int(job_data["EXIT_CODE"])

        self.result["endtime"] = job_data["FINISH_TIME"]
        self.builder.metadata["outfile"] = job_data["OUTPUT_FILE"]
        self.builder.metadata["errfile"] = job_data["ERROR_FILE"]

        self.logger.debug(f"[{self.builder.name}] result: {self.result}")
        self.logger.debug(
            f"[{self.builder.name}] returncode: {self.result['returncode']}"
        )
        self.check_test_state()

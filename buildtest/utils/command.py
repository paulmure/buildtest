import locale
import subprocess
import shlex
import shutil


class BuildTestCommand:
    """Class method to invoke shell commands and retrieve output and error.
       This class is inspired and derived from utils functions in 
       https://github.com/vsoch/scif
    """

    def __init__(self, cmd=None):

        cmd = cmd or []
        self.returncode = None
        self.out = []
        self.err = []

        # If a list isn't provided, split it
        if cmd:
            self.set_command(cmd)

    def set_command(self, cmd):
        """parse is called when a new command is provided to ensure we have
           a list. We don't check that the executable is on the path,
           as the initialization might not occur in the runtime environment.
        """
        if not isinstance(cmd, list):
            cmd = shlex.split(cmd)
        self.cmd = cmd

    def execute(self):
        """Execute a system command and return output and error.

        :param cmd: shell command to execute
        :type cmd: str, required
        :return: Output and Error from shell command
        :rtype: two str objects
        """
        # Reset the output and error records
        self.out = []
        self.err = []

        # The executable must be found, return code 1 if not
        executable = shutil.which(self.cmd[0])
        if not executable:
            self.err = ["%s not found." % self.cmd[0]]
            self.returncode = 1
            return (self.out, self.err)

        # remove the original executable
        args = self.cmd[1:]

        # Use updated command with executable and remainder (list)
        cmd = [executable] + args

        # open the process for writing
        process = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        returncode = process.poll()

        # Iterate through the output
        while returncode is None:

            out = self.decode(process.stdout.readline())
            err = self.decode(process.stderr.readline())

            # Append output and error
            if out:
                self.out.append(out)
            if err:
                self.err.append(out)
            returncode = process.poll()

        # Get the remainder of lines, add return code
        out, err = process.communicate()
        self.out += ["%s\n" % x for x in self.decode(out).split("\n") if x]
        self.err += ["%s\n" % x for x in self.decode(err).split("\n") if x]
        self.returncode = returncode

        return (self.out, self.err)

    def returnCode(self):
        """Returns the return code from shell command
        :rtype: int
        """

        return self.returncode

    def decode(self, line):
        """Given a line of output (error or regular) decode using the 
           system default, if appropriate
        """
        loc = locale.getdefaultlocale()[1]

        try:
            line = line.decode(loc)
        except:
            pass
        return line

    def get_output(self):
        """Returns the output from shell command

        :rtype: str
        """
        return self.out

    def get_error(self):
        """Returns the error from shell command

        :rtype: str
        """
        return self.err

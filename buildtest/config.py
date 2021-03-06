import logging
import os
import sys
from jsonschema import validate

from buildtest.schemas.utils import load_schema, load_recipe
from buildtest.defaults import (
    BUILDTEST_SETTINGS_FILE,
    DEFAULT_SETTINGS_FILE,
    DEFAULT_SETTINGS_SCHEMA,
)
from buildtest.system import (
    get_slurm_partitions,
    get_slurm_qos,
    get_slurm_clusters,
    get_lsf_queues,
)
from buildtest.utils.command import BuildTestCommand


logger = logging.getLogger(__name__)


def check_settings(settings_path=None, executor_check=True):
    """Checks all keys in configuration file (settings/config.yml) are valid
       keys and ensure value of each key matches expected type . For some keys
       special logic is taken to ensure values are correct and directory path
       exists.       

       If any error is found buildtest will terminate immediately.

       Parameters:

       :param settings_path: Path to buildtest settings file
       :type settings_path: str, optional

       :return: returns gracefully if all checks passes otherwise terminate immediately
       :rtype: exit code 1 if checks failed
    """

    user_schema = load_settings(settings_path)

    logger.debug(f"Loading default settings schema: {DEFAULT_SETTINGS_SCHEMA}")
    config_schema = load_schema(DEFAULT_SETTINGS_SCHEMA)

    logger.debug(f"Validating user schema with schema: {DEFAULT_SETTINGS_SCHEMA}")
    validate(instance=user_schema, schema=config_schema)
    logger.debug("Validation was successful")

    # only perform executor check if executor_check is True. This is default
    # behavior, this can be disabled only for regression test where executor check
    # such as slurm check are not applicable.
    if executor_check:
        slurm_executors = user_schema.get("executors", {}).get("slurm")
        lsf_executors = user_schema.get("executors", {}).get("lsf")

        if slurm_executors:
            validate_slurm_executors(slurm_executors)

        if lsf_executors:
            validate_lsf_executors(lsf_executors)


def load_settings(settings_path=None):
    """Load the default settings file if no argument is specified.

       Parameters:

       :param settings_path: Path to buildtest settings file
       :type settings_path: str, optional
    """

    settings_path = settings_path or BUILDTEST_SETTINGS_FILE

    if not os.path.exists(settings_path):
        settings_path = DEFAULT_SETTINGS_FILE

    # load the settings file into a schema object
    return load_recipe(settings_path)


def validate_lsf_executors(lsf_executors):
    queue_dict = get_lsf_queues()

    queue_list = []
    valid_queue_state = "Open:Active"
    record = queue_dict["RECORDS"]
    # retrieve all queues from json record
    for name in record:
        queue_list.append(name["QUEUE_NAME"])

    # check all executors have defined valid queues and check queue state.
    for executor in lsf_executors:
        queue = lsf_executors[executor].get("queue")
        # if queue field is defined check if its valid queue
        if queue:
            if queue not in queue_list:
                sys.exit(
                    f"{lsf_executors[executor]['queue']} not a valid partition!. Please select one of the following partitions: {queue_list}"
                )

            # check queue record for Status
            for name in record.keys():

                # skip record until we find matching queue
                if record[name]["QUEUE_NAME"] != queue:
                    continue

                queue_state = record[name]["STATUS"]
                # if state not Open:Active we raise error
                if not queue_state == valid_queue_state:
                    sys.exit(
                        f"{lsf_executors[executor]['queue']} is in state: {queue_state}. It must be in {valid_queue_state} state in order to accept jobs"
                    )


def validate_slurm_executors(slurm_executor):
    """This method will validate slurm executors, we check if partition, qos,
       and cluster fields are valid values by retrieving details from slurm configuration.
       These checks are performed if ``partition``, ``qos`` or ``cluster`` field
       is specified in slurm executor section.

       :param slurm_executor: list of slurm executors defined in settings['executors]['slurm'] dictionary, where settings is loaded buildtest setting
    """

    slurm_partitions = get_slurm_partitions()
    slurm_qos = get_slurm_qos()
    slurm_cluster = get_slurm_clusters()

    for executor in slurm_executor:

        # if 'partition' key defined check if its valid partition
        if slurm_executor[executor].get("partition"):

            if slurm_executor[executor]["partition"] not in slurm_partitions:
                sys.exit(
                    f"{slurm_executor[executor]['partition']} not a valid partition!. Please select one of the following partitions: {slurm_partitions}"
                )

            query = f"sinfo -p {slurm_executor[executor]['partition']} -h -O available"
            cmd = BuildTestCommand(query)
            cmd.execute()
            part_state = "".join(cmd.get_output())
            part_state = part_state.rstrip()
            # check if partition is in 'up' state. If not we raise an error.
            if part_state != "up":
                sys.exit(
                    f"{slurm_executor[executor]['partition']} is in state: {part_state}. It must be in 'up' state in order to accept jobs"
                )
        # check if 'qos' key is valid qos
        if slurm_executor[executor].get("qos"):

            if slurm_executor[executor]["qos"] not in slurm_qos:
                sys.exit(
                    f"{slurm_executor[executor]['qos']} not a valid qos! Please select one of the following qos: {slurm_qos}"
                )
        # check if 'cluster' key is valid slurm cluster
        if slurm_executor[executor].get("cluster"):

            if slurm_executor[executor]["cluster"] not in slurm_cluster:
                sys.exit(
                    f"{slurm_executor[executor]['cluster']} not a valid slurm cluster! Please select one of the following slurm clusters: {slurm_cluster}"
                )

"""
Common library functions for ApertureDB.
This will not have a big class structure, but rather a collection of functions
This is the place to put functions that are reused in codebase.
"""
import importlib
import math
import os
import sys
from typing import Callable, Optional, Tuple
import logging
import json

from aperturedb.Configuration import Configuration
from aperturedb.Connector import Connector
from aperturedb.ConnectorRest import ConnectorRest
from aperturedb.types import Blobs, CommandResponses, Commands

logger = logging.getLogger(__name__)


def import_module_by_path(filepath):
    """
    This function imports a module given a path to a python file.
    """
    module_name = os.path.basename(filepath)[:-3]
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def __create_connector(configuration: Configuration):
    if configuration.use_rest:
        connector = ConnectorRest(
            host=configuration.host,
            port=configuration.port,
            user=configuration.username,
            password=configuration.password,
            use_ssl=configuration.use_ssl,
            config=configuration)
    else:
        connector = Connector(
            host=configuration.host,
            port=configuration.port,
            user=configuration.username,
            password=configuration.password,
            use_ssl=configuration.use_ssl,
            config=configuration)
    logger.debug(
        f"Created connector using: {configuration}. Will connect on query.")
    return connector


def create_connector(name: Optional[str] = None) -> Connector:
    """
    **Create a connector to the database.**

    This function chooses a configuration in the folowing order:
    1. The configuration specified by the `name` parameter.
    2. The configuration specified by the `APERTUREDB_CONFIG` environment variable.
    3. The active configuration.

    If there are both global and local configurations with the same name, the global configuration is preferred.

    See :ref:`adb config <adb_config>`_ command-line tool for more information.

    Args:
        name (str, optional): The name of the configuration to use. Default is None.

    Returns:
        Connector: The connector to the database.
    """
    from aperturedb.cli.configure import ls
    all_configs = ls(log_to_console=False)

    def lookup_config_by_name(name: str, source: str) -> Configuration:
        if "global" in all_configs and name in all_configs["global"]:
            return all_configs["global"][name]
        if "local" in all_configs and name in all_configs["local"]:
            return all_configs["local"][name]
        assert False, f"Configuration '{name}' not found ({source})."

    if name is not None:
        config = lookup_config_by_name(name, "explicit")
    elif "APERTUREDB_CONFIG" in os.environ and os.environ["APERTUREDB_CONFIG"] != "":
        config = lookup_config_by_name(
            os.environ["APERTUREDB_CONFIG"], "envar")
    elif "active" in all_configs:
        config = lookup_config_by_name(all_configs["active"], "active")
    else:
        assert False, "No configuration found."
    return __create_connector(config)


def execute_query(q: Commands, blobs: Blobs, db: Connector,
                  success_statuses: list[int] = [0],
                  response_handler: Optional[Callable] = None, commands_per_query: int = 1, blobs_per_query: int = 0,
                  strict_response_validation: bool = False, cmd_index=None) -> Tuple[int, CommandResponses, Blobs]:
    """
    Execute a batch of queries, doing useful logging around it.
    Calls the response handler if provided.
    This should be used (without the parallel machinery) instead of db.query to keep the response handling consistent, better logging, etc.

    Args:
        q (Commands): List of commands to execute.
        blobs (Blobs): List of blobs to send.
        db (Connector): The database connector.
        success_statuses (list[int], optional): The list of success statuses. Defaults to [0].
        response_handler (Callable, optional): The response handler. Defaults to None.
        commands_per_query (int, optional): The number of commands per query. Defaults to 1.
        blobs_per_query (int, optional): The number of blobs per query. Defaults to 0.
        strict_response_validation (bool, optional): Whether to strictly validate the response. Defaults to False.

    Returns:
        int: The result code.
            - 0 : if all commands succeeded
            - 1 : if there was -1 in the response
            - 2 : For any other code.
        CommandResponses: The response.
        Blobs: The blobs.
    """
    result = 0
    logger.debug(f"Query={q}")
    r, b = db.query(q, blobs)
    logger.debug(f"Response={r}")

    if db.last_query_ok():
        if response_handler is not None:
            try:
                map_response_to_handler(response_handler,
                                        q, blobs, r, b, commands_per_query, blobs_per_query,
                                        cmd_index)
            except BaseException as e:
                logger.exception(e)
                if strict_response_validation:
                    raise e
    else:
        # Transaction failed entirely.
        logger.error(f"Failed query = {q} with response = {r}")
        result = 1

    statuses = {}
    if isinstance(r, dict):
        statuses[r['status']] = [r]
    elif isinstance(r, list):
        # add each result to a list of the responses, keyed by the response
        # code.
        [statuses.setdefault(result[cmd]['status'], []).append(result)
         for result in r for cmd in result]
    else:
        logger.error("Response in unexpected format")
        result = 1

    # last_query_ok means result status >= 0
    if result != 1:
        warn_list = []
        for status, results in statuses.items():
            if status not in success_statuses:
                for wr in results:
                    warn_list.append(wr)
        if len(warn_list) != 0:
            logger.warning(
                f"Partial errors:\r\n{json.dumps(q)}\r\n{json.dumps(warn_list)}")
            result = 2

    return result, r, b


def map_response_to_handler(handler, query, query_blobs,  response, response_blobs,
                            commands_per_query, blobs_per_query, cmd_index_offset):
    # We could potentially always call this handler function
    # and let the user deal with the error cases.
    blobs_returned = 0
    for i in range(math.ceil(len(query) / commands_per_query)):
        start = i * commands_per_query
        end = start + commands_per_query
        blobs_start = i * blobs_per_query
        blobs_end = blobs_start + blobs_per_query

        b_count = 0
        if issubclass(type(response), list):
            for req, resp in zip(query[start:end], response[start:end]):
                for k in req:
                    blob_returning_commands = ["FindImage", "FindBlob", "FindVideo",
                                               "FindDescriptor", "FindBoundingBox"]
                    if k in blob_returning_commands and "blobs" in req[k] and req[k]["blobs"]:
                        count = resp[k]["returned"]
                        b_count += count

        # The returned blobs need to be sliced to match the
        # returned entities per command in query.
        handler(
            query[start:end],
            query_blobs[blobs_start:blobs_end],
            response[start:end] if issubclass(
                type(response), list) else response,
            response_blobs[blobs_returned:blobs_returned + b_count] if
            len(response_blobs) >= blobs_returned + b_count else None,
            None if cmd_index_offset is None else cmd_index_offset + i)
        blobs_returned += b_count


def issue_deprecation_warning(old_name, new_name):
    """
    Issue a deprecation warning for a function and class.
    """
    logger.warning(
        f"{old_name} is deprecated and will be removed in a future release. Use {new_name} instead.")
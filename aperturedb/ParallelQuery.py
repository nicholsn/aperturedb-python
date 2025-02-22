from __future__ import annotations
from typing import Callable, List, Tuple, Optional
from aperturedb import Parallelizer
import numpy as np
import json
import logging
import math
import inspect


from aperturedb.DaskManager import DaskManager
from aperturedb.Connector import Connector
from aperturedb.types import Commands, Blobs, CommandResponses

logger = logging.getLogger(__name__)


def execute_batch(q: Commands, blobs: Blobs, db: Connector,
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
                ParallelQuery.map_response_to_handler(response_handler,
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


class ParallelQuery(Parallelizer.Parallelizer):
    """
    **Parallel and Batch Querier for ApertureDB**

    This class provides the abstraction for partitioning data into batches,
    so that they may be processed using different threads.

    Args:
        db (Connector): The database connector.
        dry_run (bool, optional): Whether to run in dry run mode. Defaults to False.
    """

    # 0 is success, 2 is object exists
    success_statuses = [0, 2]

    @classmethod
    def setSuccessStatus(cls, statuses: list[int]):
        cls.success_statuses = statuses

    @classmethod
    def getSuccessStatus(cls):
        return cls.success_statuses

    @classmethod
    def map_response_to_handler(cls, handler, query, query_blobs,  response, response_blobs,
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

    def __init__(self, db: Connector, dry_run: bool = False):
        super().__init__()
        test_string = f"Connection test successful with {db.config}"
        try:
            _, _ = db.query([{"GetSchema": {}}], [])
            logger.info(test_string)
        except Exception as e:
            logger.error(test_string.replace("successful", "failed"))
            raise

        self.db = db.create_new_connection()

        self.dry_run = dry_run

        self.type = "query"

        self.responses = []

        self.commands_per_query = 1
        self.blobs_per_query = 0
        self.daskManager = None
        self.batch_command = execute_batch

    def generate_batch(self, data: List[Tuple[Commands, Blobs]]) -> Tuple[Commands, Blobs]:
        """
        Here we flatten the individual queries to run them as
        a single query in a batch
        We also update the _ref values and connections refs.

        Args:
            data (list[tuple[Query, Blobs]]): The data to be batched.  Each tuple contains a list of commands and a list of blobs.

        Returns:
            commands (Commands): The batched commands.
            blobs (Blobs): The batched blobs.
        """
        def update_refs(batched_commands):
            updates = {}
            for i, cmd in enumerate(batched_commands):
                if isinstance(cmd, list):
                    # Only parallel queries will work.
                    break
                values = cmd[list(cmd.keys())[0]]
                if "_ref" in values:
                    updates[values["_ref"]] = i + 1
                    values["_ref"] = i + 1
                    assert values["_ref"] < 100000
                if "image_ref" in values:
                    values["image_ref"] = updates[values["image_ref"]]
                if "video_ref" in values:
                    values["video_ref"] = updates[values["video_ref"]]
                if "is_connected_to" in values:
                    if "ref" in values["is_connected_to"]:
                        values["is_connected_to"]["ref"] = updates[values["is_connected_to"]["ref"]]
                    for op in ["any", "all"]:
                        if op in values["is_connected_to"]:
                            for idx in range(len(values["is_connected_to"][op])):
                                if "ref" in values["is_connected_to"][op][idx]:
                                    values["is_connected_to"][op][idx]["ref"] = updates[values["is_connected_to"][op][idx]["ref"]]
                if "connect" in values and "ref" in values["connect"]:
                    values["connect"]["ref"] = updates[values["connect"]["ref"]]
                if "src" in values:
                    values["src"] = updates[values["src"]]
                if "dst" in values:
                    values["dst"] = updates[values["dst"]]
                if "ref" in values:
                    values["ref"] = updates[values["ref"]]

            return batched_commands

        q = update_refs([cmd for query in data for cmd in query[0]])
        blobs = [blob for query in data for blob in query[1]]

        return q, blobs

    def call_response_handler(self, q: Commands, blobs: Blobs, r: CommandResponses, b: Blobs):
        try:
            self.generator.response_handler(q, blobs, r, b)
        except BaseException as e:
            logger.exception(e)

    def do_batch(self, db: Connector, batch_start: int,  data: List[Tuple[Commands, Blobs]]) -> None:
        """
        Executes batch of queries and blobs in the database.

        Args:
            db (Connector): The database connector.
            data (list[tuple[Commands, Blobs]]): The data to be batched.  Each tuple contains a list of commands and a list of blobs.

        It also provides a way for invoking a user defined function to handle the
        responses of each of the queries executed. This function can be used to process
        the responses from each of the corresponding queries in [Parallelizer](/python_sdk/parallel_exec/Parallelizer)
        It will be called once per query, and it needs to have 4 parameters:
        - requests
        - input_blobs
        - responses
        - output_blobs
        Example usage:
        ``` python
            class MyQueries(QueryGenerator):
                def process_responses(requests, input_blobs, responses, output_blobs):
                    self.requests.extend(requests)
                    self.responses.extend(responses)
            loader = ParallelLoader(self.db)
            generator = MyQueries()
            loader.ingest(generator)
        ```
        """

        q, blobs = self.generate_batch(data)

        query_time = 0
        worker_stats = {}
        if not self.dry_run:
            response_handler = None
            strict_response_validation = False
            if hasattr(self.generator, "response_handler") and callable(self.generator.response_handler):
                response_handler = self.generator.response_handler
            if hasattr(self.generator, "strict_response_validation") and isinstance(self.generator.strict_response_validation, bool):
                strict_response_validation = self.generator.strict_response_validation

            # if response_handler doesn't support index, just discard the index with a wrapper.
            if response_handler is not None:
                parameter_count = len(inspect.signature(
                    response_handler).parameters)
                if parameter_count < 4 or parameter_count > 5:
                    raise Exception("Bad Signature for response_handler :"
                                    f"expected 6 > args > 3, got {parameter_count}")
                if parameter_count == 4:
                    indexless_handler = response_handler
                    def response_handler(query, qblobs, resp, rblobs, qindex): return indexless_handler(
                        query, qblobs, resp, rblobs)

            result, r, b = self.batch_command(
                q,
                blobs,
                db,
                ParallelQuery.success_statuses,
                response_handler,
                self.commands_per_query,
                self.blobs_per_query,
                strict_response_validation=strict_response_validation,
                cmd_index=batch_start)
            if result == 0:
                query_time = db.get_last_query_time()
                worker_stats["succeeded_commands"] = len(q)
                worker_stats["succeeded_queries"] = len(data)
                worker_stats["objects_existed"] = sum(
                    [v['status'] == 2 for i in r for k, v in i.items()])
            elif result == 1:
                self.error_counter += 1
                worker_stats["succeeded_queries"] = 0
                worker_stats["succeeded_commands"] = 0
                worker_stats["objects_existed"] = 0
            elif result == 2:
                # with result 2, some queries might have failed.
                def filter_per_group(group):
                    return group.items() if isinstance(group, dict) else {}
                worker_stats["succeeded_commands"] = sum(
                    [v['status'] == 0 for i in r for k, v in filter_per_group(i)])
                worker_stats["objects_existed"] = sum(
                    [v['status'] == 2 for i in r for k, v in filter_per_group(i)])
                sq = 0
                for i in range(0, len(r), self.commands_per_query):
                    # Some errors stop the whole query from being executed
                    # https://docs.aperturedata.io/query_language/Overview/Responses#return-status
                    if issubclass(type(r), list):
                        if all([v['status'] == 0 for j in r[i:i + self.commands_per_query] for k, v in filter_per_group(j)]):
                            sq += 1
                worker_stats["succeeded_queries"] = sq
        else:
            query_time = 1

        # append is thread-safe
        self.times_arr.append(query_time)
        self.actual_stats.append(worker_stats)

    def worker(self, thid: int, generator, start: int, end: int):
        # A new connection will be created for each thread
        db = self.db.create_new_connection()

        total_batches = (end - start) // self.batchsize

        if (end - start) % self.batchsize > 0:
            total_batches += 1

        logger.info(f"Worker {thid} executing {total_batches} batches")
        for i in range(total_batches):

            batch_start = start + i * self.batchsize
            batch_end = min(batch_start + self.batchsize, end)

            try:
                self.do_batch(db, batch_start,
                              generator[batch_start:batch_end])
            except Exception as e:
                logger.exception(e)
                logger.warning(
                    f"Worker {thid} failed to execute batch {i}: [{batch_start},{batch_end}]")
                self.error_counter += 1

            if thid == 0 and self.stats:
                self.pb.update((i + 1) / total_batches)

    def get_objects_existed(self) -> int:
        return sum([stat["objects_existed"]
                    for stat in self.actual_stats])

    def get_succeeded_queries(self) -> int:
        return sum([stat["succeeded_queries"]
                    for stat in self.actual_stats])

    def get_succeeded_commands(self) -> int:
        return sum([stat["succeeded_commands"]
                    for stat in self.actual_stats])

    def query(self, generator, batchsize: int = 1, numthreads: int = 4, stats: bool = False) -> None:
        """
        This function takes as input the data to be executed in specified number of threads.
        The generator yields a tuple : (array of commands, array of blobs)
        Args:
            generator (_type_): The class that generates the queries to be executed.
            batchsize (int, optional): Number of queries per transaction. Defaults to 1.
            numthreads (int, optional): Number of parallel workers. Defaults to 4.
            stats (bool, optional): Show statistics at end of ingestion. Defaults to False.
        """

        use_dask = hasattr(generator, "use_dask") and generator.use_dask
        if use_dask:
            self._reset(batchsize=batchsize, numthreads=numthreads)
            self.daskmanager = DaskManager(num_workers=numthreads)

        if hasattr(self, "query_setup"):
            self.query_setup(generator)

        if use_dask:
            results, self.total_actions_time = self.daskmanager.run(
                self.__class__, self.db, generator, batchsize, stats=stats)
            self.actual_stats = []
            for result in results:
                if result is not None:
                    self.times_arr.extend(result.times_arr)
                    self.error_counter += result.error_counter
                    self.actual_stats.append(
                        {"succeeded_queries": result.succeeded_queries,
                         "succeeded_commands": result.succeeded_commands,
                         "objects_existed": result.objects_existed})
            self.total_actions = len(generator.df)

            if stats:
                self.print_stats()
        else:
            # allow subclass to do verification
            if issubclass(type(self), ParallelQuery) and hasattr(self, 'verify_generator') and callable(self.verify_generator):
                self.verify_generator(generator)
            elif len(generator) > 0:
                if isinstance(generator[0], tuple) and isinstance(generator[0][0], list):
                    # if len(generator[0]) > 0:
                    #
                    #  Not applicable to old style loaders.
                    self.commands_per_query = len(generator[0][0])
                    if len(generator[0][1]):
                        self.blobs_per_query = len(generator[0][1])
                else:
                    logger.error(
                        f"Could not determine query structure from:\n{generator[0]}")
                    logger.error(type(generator[0]))
            logger.info(
                f"Commands per query = {self.commands_per_query}, Blobs per query = {self.blobs_per_query}"
            )
            self.run(generator, batchsize, numthreads, stats)

    def print_stats(self) -> None:

        times = np.array(self.times_arr)
        total_queries_exec = len(times)

        print("============ ApertureDB Parallel Query Stats ============")
        print(f"Total time (s): {self.total_actions_time}")
        print(f"Total queries executed: {total_queries_exec}")

        if total_queries_exec == 0:
            print("All queries failed!")

        else:
            mean = np.mean(times)
            std = np.std(times)
            tp = 0 if mean == 0 else 1 / mean * self.numthreads

            print(f"Avg Query time (s): {mean}")
            print(f"Query time std: {std}")
            print(f"Avg Query Throughput (q/s): {tp}")

            i_tp = self.total_actions / self.total_actions_time
            print(
                f"Overall insertion throughput ({self.type}/s): {i_tp if self.error_counter == 0 else 'NaN'}")

            if self.error_counter > 0:
                err_perc = 100 * self.error_counter / total_queries_exec
                print(f"Total errors encountered: {self.error_counter}")
                print(f"Errors (%): {err_perc}")

        print("=========================================================")

    def debug_sample(self, **kwargs) -> None:
        """
        Sample the data to be ingested for debugging purposes.
        """
        if "sample_count" in kwargs:
            sample_count = kwargs["sample_count"]
        else:
            sample_count = len(self.generator)

        if sample_count > 0:
            print("Sample of data to be ingested:")
            for i in range(sample_count):
                print(self.generator[i])

import os
import pickle
import time
import zlib

from fuzzowski import Target, FuzzLogger, exception
from fuzzowski.loggers import FuzzLoggerText
from fuzzowski.restarters import IRestarter
from . import helpers
from . import constants
from fuzzowski.graph import Graph, Edge
from fuzzowski.mutants.blocks import Request
from fuzzowski.mutants import Mutant
from typing import List, Generator, Dict
from .testcase import TestCase
from fuzzowski.prompt.session_prompt import SessionPrompt


class SessionOptions(object):
    """
    This is a dumb, auxiliary class to save all session options
    """
    def __init__(self, *args, **kwargs):
        self.crash_threshold_element = None
        self.crash_threshold_request = None
        self.restart_interval = None
        self.sleep_time = None
        self.__dict__.update(kwargs)


class Session(object):
    """
    Implements main fuzzing functionality, contains all configuration parameters, etc.

    Args:
        graph: Graph with all the connected requests
        session_filename (str): Filename to serialize persistent data to. Default None.
        sleep_time (float):     Time in seconds to sleep in between tests. Default 0.
        restart_interval (int): Restart the target after n test cases, disable by setting to 0 (default).
        crash_threshold_request (int):  Maximum number of crashes allowed before a request is exhausted. Default 12.
        crash_threshold_element (int):  Maximum number of crashes allowed before an element is exhausted. Default 3.
        restart_sleep_time (float): Time in seconds to sleep when target can't be restarted. Default 5.
        fuzz_loggers (list of ifuzz_logger.IFuzzLogger): For saving test data and results.. Default Log to STDOUT.
        receive_data_after_each_request (bool): If True, Session will attempt to receive a reply after transmitting
                                                each non-fuzzed node. Default True.
        check_data_received_each_request (bool): If True, Session will verify that some data has
                                                 been received after transmitting each non-fuzzed node, and if not,
                                                 register a failure. If False, this check will not be performed. Default
                                                 False. A receive attempt is still made unless
                                                 receive_data_after_each_request is False.
        receive_data_after_fuzz (bool): If True, Session will attempt to receive a reply after transmitting
                                        a fuzzed message. Default False.
        ignore_connection_reset (bool): Log ECONNRESET errors ("Target connection reset") as "info" instead of
                                failures.
        ignore_connection_aborted (bool): Log ECONNABORTED errors as "info" instead of failures.
        ignore_connection_issues_after_fuzz (bool): Ignore fuzz data transmission failures. Default True.
                                This is usually a helpful setting to enable, as targets may drop connections once a
                                message is clearly invalid.
        target (Target):        Target for fuzz session. Target must be fully initialized. Default None.
        restarter (IRestarter): Restarter module initialized. Will call restart() when the target is down. Default None
        monitors (list of IMonitor): Monitor modules
        new_connection_between_requests: bool = True. Close and Open the connection to the target between packets
        transmit_full_path: bool = False. Transmit the next node of the graph when fuzzing a node
    """

    def __init__(self, graph: Graph = None,
                 session_filename: str = None,
                 sleep_time: float = 0.0,
                 restart_interval: int = 0,
                 crash_threshold_request: int = 5,
                 crash_threshold_element: int = 3,
                 restart_sleep_time: float = 5.0,
                 fuzz_loggers: "list of FuzzLogger" = None,
                 receive_data_after_each_request: bool = True,
                 check_data_received_each_request: bool = False,
                 receive_data_after_fuzz: bool = False,
                 ignore_connection_reset: bool = False,  # TODO: Delete
                 ignore_connection_aborted: bool = False,  # TODO: Delete
                 ignore_transmission_errors: bool = True,
                 ignore_connection_issues_after_fuzz: bool = True,
                 target: Target = None,
                 restarter: IRestarter = None,
                 monitors: "list of IMonitor" = [],
                 new_connection_between_requests: bool = False,
                 transmit_full_path: bool = False,
                 tests_number_to_keep: int = 1000
                 ):
        super().__init__()

        self.opts = SessionOptions(sleep_time=sleep_time,
                                   restart_interval=restart_interval,
                                   restart_sleep_time=restart_sleep_time,

                                   # Crash limits to ignore elements
                                   crash_threshold_request=crash_threshold_request,
                                   crash_threshold_element=crash_threshold_element,

                                   # Transmission Options
                                   transmit_full_path=transmit_full_path,
                                   new_connection_between_requests=new_connection_between_requests,
                                   receive_data_after_each_request=receive_data_after_each_request,
                                   check_data_received_each_request=check_data_received_each_request,
                                   receive_data_after_fuzz=receive_data_after_fuzz,

                                   ignore_transmission_errors=ignore_transmission_errors,  # TODO
                                   ignore_connection_issues_after_fuzz=ignore_connection_issues_after_fuzz,  # TODO

                                   tests_number_to_keep=tests_number_to_keep
                                   )

        # Create Results Dir if it does not exist
        helpers.mkdir_safe(os.path.join(constants.RESULTS_DIR))

        # Make default logger if no others set
        if fuzz_loggers is None:
            fuzz_loggers = [FuzzLoggerText()]

        # Open session file if specified
        if session_filename is not None:
            self.session_filename = os.path.join(constants.RESULTS_DIR, session_filename)
            self.log_filename = os.path.join(constants.RESULTS_DIR, ''.join(session_filename.split('.')[:-1]) + '.log')
            fuzz_loggers.append(FuzzLoggerText(file_handle=open(self.log_filename, 'a')))
        else:
            self.session_filename = None

        self.logger = FuzzLogger(fuzz_loggers)

        if self.session_filename is not None:
            self.logger.log_info('Using session file: {}'.format(self.session_filename))

        self.target = target
        if target is not None:
            try:
                self.add_target(target)
            except exception.FuzzowskiRpcError as e:  # TODO: Change exception
                self.logger.log_error(str(e))
                raise
        self._requests = []
        if graph is not None:
            self.graph = graph
            if len(self.graph.graph_dict) == 0:
                raise exception.FuzzowskiRuntimeError('The Graph must have at least 1 request!')
        else:
            self.graph = Graph()

        self.suspects: Dict[int, TestCase or None] = {}  # Dictionary of suspect test cases
        self.disabled_elements: Dict[str, 'Mutant'] = {}  # Dictionary of disabled Mutants or Requests
        self.latest_tests = []  # List of N test cases

        self._restarter = restarter

        self.monitors = []
        for monitor_class in monitors:
            self.monitors.append(monitor_class(self))
            # TODO: How to pass arbitrary args to monitors? think a good way!
            #  Maybe passing all the args and let the monitor pick them?


        # Some variables that will be used during fuzzing
        self.last_send = None
        self.last_recv = None
        self.mutant_index = 0
        self._test_cases = None
        self.test_case = None

        self.is_paused = True
        self.total_mutations = self.num_mutations
        self.prompt = None
        # self.prompt = SessionPrompt(self) # Placed in start() to avoid tests failing due lack of input

    # ================================================================#
    # Actions                                                         #
    # ================================================================#

    def start(self):
        """
        Starts the prompt once the session is prepared
        """
        self.prompt = SessionPrompt(self)
        self.reset()
        self.import_file(self.session_filename)
        self.prompt.start_prompt()

    def run(self):
        """
        Run the actual test case or the test case selected with the ID
        Args:
            test_case_id: The test case that will be run. if None the actual one will be used.
        """
        if self.test_case is not None:
            self.test_case.run()
            self.add_latest_test(self.test_case)
            self.check_monitors()
            # self.process_errors()  # TODO: Move add suspects from different parts of the code to this function!
        else:
            self.logger.log_info('Nothing to run yet.')

    # --------------------------------------------------------------- #

    def run_all(self):
        """
        This is the main iterator of test cases, it goes through all the test cases, running each one of them
        The cases when this function stops running test cases are:
            - When self.is_paused is set to True
            - When all test_cases in the self._test_cases iterator are exhausted

        self._test_cases or self.test_case should be set before calling this function

        This is usually called from the command prompt and the is_pause flag controlled from there.
        """
        while not self.is_paused:
            self.run_next(force=False)

            # When running all test cases, sleep between test cases (if sleep_time is set)
            if self.opts.sleep_time > 0:
                self.logger.open_test_step("Sleep between tests.")
                self.logger.log_info(f"sleeping for {self.opts.sleep_time} seconds")
                time.sleep(self.opts.sleep_time)

            # When running all test cases and test_interval is set, check if you need to restart the target
            if self.opts.restart_interval > 0 and self.test_case.id % self.opts.restart_interval == 0:
                self.logger.open_test_step(f"Restart interval of {self.opts.restart_interval} reached")
                self.restart_target()

    # --------------------------------------------------------------- #

    def run_next(self, force: bool = False):
        """
        Run the next test_case and go to the next one

        Args:
            force: if True, the test case will be run even if it is disabled.
        """
        if self.test_case is not None:
            if force or not self.test_case.disabled:
                self.run()

        self.next()
        if self.test_case is None:  # All test cases exhausted
            self.logger.log_info('Fuzzing test cases exhausted!')
            self.is_paused = True

    # --------------------------------------------------------------- #

    def reset(self):
        self._reset()

    def _reset(self):
        self.mutant_index = 0
        self.last_send = None
        self.last_recv = None
        self._test_cases = self.test_case_iterator()
        self.test_case = None
        for path in self.graph.path_iterator():
            for edge in path:
                request = edge.dst
                request.reset()

    # --------------------------------------------------------------- #

    def test(self, test_case_id: int = None):
        try:
            mutant_index = self.mutant_index
            if test_case_id is None:
                if mutant_index != 0:
                    self.test_case.test()
                    return
                else:
                    test_case_id = 1
            self.goto(test_case_id)
            self.test_case.test()
            self.goto(mutant_index)
        except exception.FuzzowskiTargetConnectionFailedError as e:
            self.logger.log_fail(f"Test failed: {type(e).__name__}. {str(e)}")

    # ================================================================#
    # Movements                                                       #
    # ================================================================#

    def goto(self, test_case_id: str or int) -> TestCase or None:
        """
        Prepare the session, self.test_case and self._test_cases in the test_case with the test_case_id specified.
        Args:
            test_case_id: The test_case to go. 0 go to an state of a new session. It can also be a path!

        Returns: The test_case specified by test_case_id, or None if test_case_id is 0
        """
        if type(test_case_id) is int:
            return self.goto_id(test_case_id)
        else:
            return self.goto_path(test_case_id)

    def goto_id(self, test_case_id: int) -> TestCase or None:
        """
        Prepare the session, self.test_case and self._test_cases in the test_case with the test_case_id specified.
        Args:
            test_case_id: The test_case to go. 0 go to an state of a new session.

        Returns: The test_case specified by test_case_id, or None if test_case_id is 0
        """
        if test_case_id > self.total_mutations:
            test_case_id = self.total_mutations
        # 1st. Reset all
        self._reset()
        if test_case_id == 0:
            return None
        for test_case in self._test_cases:
            if test_case.id == test_case_id:
                return test_case

    def goto_path(self, path_name) -> TestCase or None:
        """
        Prepare the session, self.test_case and self._test_cases in the test_case with the path_name identified.
        Args:
            path_name: The test_case to go. It must be "request_name" or "request_name.mutant_name"

        Returns: The first test_case specified by path_name
        """
        destination = Request.get_mutant_by_path(path_name)
        if not destination.fuzzable:
            raise exception.FuzzowskiRuntimeError(f"You can't go to {path_name}. It is not fuzzable!")
        self._reset()
        for test_case in self._test_cases:
            if test_case.request == destination or test_case.request.mutant == destination:
                return test_case

    # --------------------------------------------------------------- #

    def skip(self) -> TestCase or None:
        """Skip the current mutant, go to the next mutant"""
        if self.test_case is None:
            return self.next()
        else:
            test_case_mutant = self.test_case.request.mutant
            while self.test_case is not None and test_case_mutant == self.test_case.request.mutant:
                self.next()
            return self.test_case

    # --------------------------------------------------------------- #

    def next(self) -> TestCase or None:
        """
        Returns the next test case. Resets the state of the session if test_cases are exhausted

        Returns: The next test case in self._test_cases, or None if self._test_cases is exhausted
        """
        try:
            self.test_case = next(self._test_cases)
            return self.test_case
        except StopIteration:
            self.reset()
            return None  # All test cases exhausted

    # --------------------------------------------------------------- #

    # ================================================================#
    # Test case Iterators                                             #
    # ================================================================#

    def test_case_iterator(self) -> Generator[TestCase, None, None]:
        """
        A generator of all session TestCases
        """
        self.mutant_index = 0
        for path in self.graph.path_iterator():
            yield from self.test_case_path_iterator(path)

    def test_case_path_iterator(self, path: List[Edge]) -> Generator[TestCase, None, None]:
        """
        A generator of all TestCases for an specified path

        Args:
            path: the path to get the cases from
        """
        for edge in path:
            mutant_request = edge.dst  # First, we chose our mutant Request
            yield from self.test_case_request_iterator(mutant_request, path)

    def test_case_request_iterator(self, request: Request, path: List[Edge]) -> Generator[TestCase, None, None]:
        """
        A generator of all TestCases for an specified request in a path

        Args:
            request: Request that is being fuzzed
            path: Path the request belongs to

        Returns:
        """
        for _ in request:
            self.mutant_index += 1
            self.test_case = TestCase(id=self.mutant_index, session=self, request=request, path=path)
            yield self.test_case

    # ================================================================#
    # Graph related functions                                         #
    # =====================================================   ===========#

    def connect(self, src: Request, dst: Request = None, callback: callable = None):
        self.graph.connect(src, dst, callback)
        if type(src) is Request and src not in self._requests:
            self._requests.append(src)
        if type(dst) is Request and dst not in self._requests:
            self._requests.append(dst)
        self.total_mutations = self.num_mutations

    # ================================================================#
    # Suspects, disabled elements                                     #
    # ================================================================#

    def add_suspect(self, test_case):
        if test_case.id not in self.suspects:
            self.suspects[test_case.id] = test_case
            self.logger.log_info(f'Added test case {test_case.id} as a suspect')

            # Check if crash threshold
            request_crashes = {}
            mutant_crashes = {}
            for suspect in self.suspects.values():
                if suspect is not None:
                    request_name = suspect.request.name
                    request_crashes[request_name] = request_crashes.get(request_name, 0) + 1
                    if request_crashes[request_name] >= self.opts.crash_threshold_request:
                        # Disable request! :o
                        self.logger.log_fail(f'Crash threshold reached for request {request_name}. Disabling it')
                        self.disable_by_path_name(request_name)

                    mutant_name = suspect.request.mutant.name
                    mutant_crashes[mutant_name] = mutant_crashes.get(mutant_name, 0) + 1
                    if mutant_crashes[mutant_name] >= self.opts.crash_threshold_element:
                        # Disable mutant! :o
                        self.logger.log_fail(f'Crash threshold reached for mutant {request_name}.{mutant_name}. '
                                              f'Disabling it')
                        self.disable_by_path_name(f'{request_name}.{mutant_name}')

    def add_last_case_as_suspect(self, error):
        if len(self.latest_tests) == 0:
            return  # No latest case to add
        latest_test = self.latest_tests[0]
        latest_test.add_error(error)
        self.add_suspect(latest_test)

    # --------------------------------------------------------------- #

    def disable_current_mutant(self, disable=True):
        if self.test_case is not None:
            self.test_case.request.mutant.disabled = disable

    def disable_current_request(self, disable=True):
        if self.test_case is not None:
            self.test_case.request.disabled = disable

    def disable_by_path_name(self, path_name, disable=True):
        disabled_element = Request.get_mutant_by_path(path_name)
        disabled_element.disabled = disable
        if disable:  # Add to self.disabled_elements dictionary
            self.disabled_elements[path_name] = disabled_element
        else:
            try:
                self.disabled_elements.pop(path_name)
            except KeyError:
                pass

    def add_latest_test(self, test_case):
        """ Add a test case to the list of latest test cases keeping the maximum number"""
        if len(self.latest_tests) == self.opts.tests_number_to_keep:
            self.latest_tests.pop() # Take latest test
        self.latest_tests.insert(0, test_case)

    # ================================================================#
    # Restarters, Monitors                                            #
    # ================================================================#

    def restart_target(self):
        """ It will call the restart() command of the IRestarter instance, if a restarter module was set"""
        if self._restarter is not None:
            try:
                self.logger.open_test_step('Restarting Target')
                restarter_info = self._restarter.restart()
                self.logger.log_info(restarter_info)
            except Exception as e:
                self.logger.log_fail(
                    "The Restarter module {} threw an exception: {}".format(self._restarter.name(), e))

    def check_monitors(self):
        """ Check all monitors, and add the current test case as a suspect if a monitor returns False """
        # TODO: Move the create suspects to the monitor itself
        for monitor in self.monitors:
            monitor_success = monitor.run()
            if not monitor_success:
                self.add_suspect(self.test_case)

    # --------------------------------------------------------------- #

    # ================================================================#
    # Search                                                          #
    # ================================================================#

    # def _search_elem_by_path(self, element_path):
    #     request_name, element_name = element_path.split('.')
    #     request = self.find_node("name", request_name)
    #     return self._search_elem_by_name(request, element_name)
    #
    # def _search_elem_by_name(self, block, name):
    #     if block.name == name:
    #         return block
    #
    #     if hasattr(block, 'stack'):
    #         for elem in block.stack:
    #             bl = self._search_elem_by_name(elem, name)
    #             if bl is not None:
    #                 return bl
    #     return None

    # ================================================================#
    # Session properties functions                                    #
    # ================================================================#

    def save_session_state(self) -> dict:
        state = {
            "mutant_index": self.mutant_index,
            "suspect_ids": [key for key in self.suspects.keys()],
            "disabled_names": [key for key in self.disabled_elements.keys()]
            # TODO: crashes, last recv...
        }
        return state

    # --------------------------------------------------------------- #

    def load_session_state(self, state: dict) -> None:
        self.goto(state['mutant_index'])
        for suspect_id in state['suspect_ids']:
            if suspect_id not in self.suspects:
                self.suspects[suspect_id] = None  # TODO: Adding as empty for now
        for mutant_name in state['disabled_names']:
            try:
                self.disable_by_path_name(mutant_name, disable=True)
            except exception.FuzzowskiRuntimeError:
                pass

    # --------------------------------------------------------------- #

    def export_file(self, session_filename=None):
        """
        Dump various session values to disk.

        @see: import_file()
        """
        if session_filename is None:
            session_filename = self.session_filename

        if not session_filename:
            return

        data = self.save_session_state()

        fh = open(session_filename, "wb+")
        fh.write(zlib.compress(pickle.dumps(data, protocol=2)))
        fh.close()

    def import_file(self, session_filename=None):
        """
        Load various session values from disk.

        @see: export_file()
        """
        if session_filename is None:
            session_filename = self.session_filename

        if session_filename is None:
            return

        try:
            with open(session_filename, "rb") as f:
                data = pickle.loads(zlib.decompress(f.read()))
        except (IOError, zlib.error, pickle.UnpicklingError):
            return
        self.load_session_state(data)

    # --------------------------------------------------------------- #

    def add_target(self, target):
        """
        Add a target to the session. Multiple targets can be added for parallel fuzzing.

        Args:
            target (Target): Target to add to session
        """
        target.set_fuzz_data_logger(fuzz_data_logger=self.logger)

        # add target to internal list.
        self.target = target

    @property
    def num_mutations(self):
        """
        Number of total mutations in the graph. The logic of this routine is identical to that of fuzz(). See fuzz()
        for inline comments. The member variable self.total_num_mutations is updated appropriately by this routine.
        """
        num_mutations = 0
        for path in self.graph.path_iterator():
            for edge in path:
                request = edge.dst
                num_mutations += request.num_mutations
        return num_mutations

    def __iter__(self):
        self.reset()
        return self

    def __next__(self):
        return self.next()
""" Where jobs run: inline, on threads or in processes. All are
`concurrent.futures.Executor`s, so the runner doesn't care which """
import concurrent.futures as cf
import multiprocessing
from typing import Callable, Tuple


class EagerExecutor(cf.Executor):
    """ Runs each call in the calling thread before `submit` returns, with
    exceptions stored in the future. For tests and one-shot scripts """

    def submit(self, fn, /, *args, **kwargs) -> cf.Future:
        future = cf.Future()
        future.set_running_or_notify_cancel()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)
        return future


def make_executor(mode: str, workers: int, initializer: Callable = None,
                  initargs: Tuple = ()) -> cf.Executor:
    """ `mode` is 'eager', 'thread' or 'process'. Processes are started by
    spawning, since forking a process that has threads and database
    connections isn't safe; `initializer` runs once in each worker """
    if mode == 'eager':
        if initializer is not None:
            initializer(*initargs)
        return EagerExecutor()
    if mode == 'thread':
        return cf.ThreadPoolExecutor(max_workers=workers,
                                     thread_name_prefix='job',
                                     initializer=initializer,
                                     initargs=initargs)
    if mode == 'process':
        return cf.ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context('spawn'),
            initializer=initializer, initargs=initargs)
    raise ValueError(f"Unknown job executor {mode!r}: use eager, thread "
                     "or process")

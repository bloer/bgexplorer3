""" The registry of things a job can do """
from dataclasses import dataclass
from typing import Callable, Optional, Tuple, Type


@dataclass(frozen=True)
class JobKind:
    name: str
    func: Callable
    # at most one serial job runs at a time for each scope
    serial: bool = False
    # runs alone in its scope: waits for running serial and exclusive jobs,
    # and holds back serial jobs submitted after it
    exclusive: bool = False
    # safe to run again from the start if the process running it dies
    idempotent: bool = False
    max_attempts: int = 3
    # exceptions that requeue the job, without counting an attempt
    retry_on: Tuple[Type[BaseException], ...] = ()
    label: Optional[str] = None


_KINDS = {}


def job_kind(name: str, *, serial: bool = False, exclusive: bool = False,
             idempotent: bool = False, max_attempts: int = 3,
             retry_on: Tuple[Type[BaseException], ...] = (),
             label: Optional[str] = None):
    """ Decorator registering `func(params: dict) -> Optional[dict]` as the
    job kind `name`. The returned dict (JSON types only) becomes the result.
    Registering a name again replaces the earlier kind, so that modules can
    be reloaded """
    def register(func):
        _KINDS[name] = JobKind(name, func, serial, exclusive, idempotent,
                               max_attempts, tuple(retry_on), label)
        return func
    return register


def get_kind(name: str) -> JobKind:
    """ Raises KeyError if no kind is registered as `name` """
    try:
        return _KINDS[name]
    except KeyError:
        raise KeyError(f"No job kind named {name!r}") from None

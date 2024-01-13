from typing import Any, Generic, TypeVar
from rx.subject import BehaviorSubject
from rx import Observable

T = TypeVar('T')


class ZZStream(Generic[T]):
    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        self.subject = BehaviorSubject[T](None)

    def publish(self, data: T) -> None:
        """
        Publish data to the stream.
        """
        self.subject.on_next(data)

    def subscribe(self) -> Observable[T]:
        """
        Subscribe to the stream.
        """
        return self.subject.as_observable()

    def get_last_value(self) -> T:
        """
        Get the last value published to the stream.
        """
        return self.subject.value

    def complete(self) -> None:
        """
        Complete the stream.
        """
        self.subject.on_completed()

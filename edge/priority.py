"""Zone-wise GPU priority (one GPU, several cameras).

Without this, the cameras take turns on the GPU in arrival order, so with several busy cameras a
high-risk zone waits behind a medium/low-risk one and its alert comes late. PriorityGate lets only one
camera run its models at a time and, when the GPU becomes free, gives it to the waiting camera with
the most important zone (rank 0 = high risk, 1 = medium, 2 = low); equal ranks go first come first served.
A low-risk camera is never starved: a frame that has waited `max_wait_s` is treated as top priority.
"""
import heapq
import itertools
import threading
import time
from contextlib import contextmanager

RANK = {"high": 0, "medium": 1, "low": 2}


class PriorityGate:
    def __init__(self, max_wait_s=0.6):
        self.cv = threading.Condition()
        self.busy = False
        self.waiting = []                    # heap of [rank, seq, since]
        self.seq = itertools.count()
        self.max_wait = max_wait_s
        self.served = {}                     # rank -> count (selftest / debugging)

    def _head(self):
        now = time.time()
        for w in self.waiting:               # aged entries jump the queue (no starvation)
            if w[0] > -1 and now - w[2] >= self.max_wait:
                w[0] = -1
        heapq.heapify(self.waiting)
        return self.waiting[0]

    @contextmanager
    def slot(self, rank):
        me = [rank, next(self.seq), time.time()]
        with self.cv:
            heapq.heappush(self.waiting, me)
            while self.busy or self._head() is not me:
                self.cv.wait(timeout=0.05)
            heapq.heappop(self.waiting)
            self.busy = True
        try:
            yield
        finally:
            with self.cv:
                self.busy = False
                self.served[rank] = self.served.get(rank, 0) + 1
                self.cv.notify_all()


GATE = PriorityGate()

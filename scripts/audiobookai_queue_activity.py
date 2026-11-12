"""Recent Studio queue transitions and progress watchdog state."""
from collections import deque
from time import monotonic, strftime


class QueueActivity:
    STALL_SECONDS = {
        'handoff': 30,
        'launching': 120,
        'preparing': 300,
        'narrating': 600,
        'exporting': 900,
        'verifying': 300,
    }

    def __init__(self, clock=monotonic):
        self.clock = clock
        self.events = deque(maxlen=40)
        self.phase = 'idle'
        self.job_id = None
        self.message = 'Queue idle'
        self.last_progress = clock()
        self.export_seconds = -1
        self.export_percent = 0
        self.stalled = False

    def transition(self, phase, message, job_id=None):
        if (phase, message, job_id) == (self.phase, self.message, self.job_id):
            return False
        self.phase, self.message, self.job_id = phase, message, job_id
        self.last_progress = self.clock()
        self.stalled = False
        if phase == 'exporting':
            self.export_seconds = -1
            self.export_percent = 0
        self.record(message, job_id)
        return True

    def record(self, message, job_id=None):
        job = f' · #{job_id}' if job_id is not None else ''
        self.events.append(f'{strftime("%H:%M:%S")}{job} · {message}')

    def progress(self):
        self.last_progress = self.clock()
        if self.stalled:
            self.stalled = False
            self.record('Progress resumed', self.job_id)
            return True
        return False

    def export_progress(self, seconds, percent):
        if self.phase != 'exporting' or seconds <= self.export_seconds:
            return False
        self.export_seconds = seconds
        self.export_percent = percent
        return self.progress()

    def check_stall(self):
        limit = self.STALL_SECONDS.get(self.phase)
        if limit is None or self.stalled or self.clock() - self.last_progress < limit:
            return False
        self.stalled = True
        self.record(self.status, self.job_id)
        return True

    @property
    def status(self):
        if self.stalled:
            minutes, seconds = divmod(self.STALL_SECONDS[self.phase], 60)
            duration = f'{minutes} min' if minutes else f'{seconds} sec'
            return f'{self.message} · No progress for at least {duration}'
        return self.message

"""Reduce third-party Python diagnostics to safe local metadata at startup."""
import logging
import queue
import threading

from .logging import SafeJSONLLogger, safe_error_class


class SafeLibraryHandler(logging.Handler):
    def __init__(self, logger, service):
        super().__init__(logging.WARNING)
        self.safe_logger, self.service = logger, service
        self.records = queue.Queue(maxsize=128)
        self.dropped = 0
        self.writer = threading.Thread(target=self._write, name='local-safe-diagnostics', daemon=True)
        self.writer.start()

    def _write(self):
        while True:
            error_class = self.records.get()
            try:
                if error_class is None:
                    return
                self.safe_logger.emit('service_failed', service=self.service, error_class=error_class)
            except Exception:
                pass
            finally:
                self.records.task_done()

    def emit(self, record):
        # Never call getMessage(), formatException(), str(error) or repr(args).
        # Library warnings/errors may include HTTP bodies, URLs or credentials.
        try:
            error = record.exc_info[1] if record.exc_info else None
            self.records.put_nowait(safe_error_class(error))
        except queue.Full:
            self.dropped += 1
        except Exception:
            pass

    def close(self):
        try:
            self.records.put_nowait(None)
        except queue.Full:
            pass
        self.writer.join(timeout=.25)
        super().close()


def install_safe_standard_logging(data_dir, service):
    logger = SafeJSONLLogger(data_dir / 'logs' / (service + '.jsonl'))
    handler = SafeLibraryHandler(logger, service)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
    # Uvicorn's default log config must not replace these after installation.
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'httpx', 'httpcore',
                 'asyncio', 'langchain', 'langsmith', 'langgraph'):
        current = logging.getLogger(name)
        current.handlers = []
        current.propagate = True
    return logger

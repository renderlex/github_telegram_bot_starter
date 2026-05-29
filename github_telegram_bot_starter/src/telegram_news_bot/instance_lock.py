from __future__ import annotations

import msvcrt
from pathlib import Path


class InstanceLockError(RuntimeError):
	pass


class InstanceLock:
	def __init__(self, lock_path: Path) -> None:
		self._lock_path = lock_path
		self._handle = None

	def __enter__(self) -> "InstanceLock":
		self.acquire()
		return self

	def __exit__(self, exc_type, exc, tb) -> None:
		self.release()

	def acquire(self) -> None:
		self._lock_path.parent.mkdir(parents=True, exist_ok=True)
		handle = self._lock_path.open("a+b")
		try:
			handle.seek(0)
			if handle.tell() == 0:
				handle.write(b"0")
				handle.flush()
			handle.seek(0)
			msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
		except OSError as exc:
			handle.close()
			raise InstanceLockError("Another serve process is already running.") from exc

		handle.seek(0)
		handle.truncate()
		handle.write(str(__import__("os").getpid()).encode("ascii", errors="ignore"))
		handle.flush()
		self._handle = handle

	def release(self) -> None:
		if self._handle is None:
			return

		try:
			self._handle.seek(0)
			msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
		finally:
			self._handle.close()
			self._handle = None

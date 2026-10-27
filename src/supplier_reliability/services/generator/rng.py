"""Named, order-independent random streams."""

import zlib

import numpy as np
from faker import Faker


class RandomSource:
    """All generator randomness comes from here.

    ``stream(name)`` returns a PCG64 Generator seeded from ``[seed, crc32(name)]``. The same
    Generator instance is cached per name, so a stream *continues* across calls (it is not
    reset). Because each stream depends only on (seed, name), adding a stream never shifts others.
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._streams: dict[str, np.random.Generator] = {}
        self._fakers: dict[str, Faker] = {}

    @staticmethod
    def _key(name: str) -> int:
        return zlib.crc32(name.encode("utf-8"))

    def stream(self, name: str) -> np.random.Generator:
        if name not in self._streams:
            ss = np.random.SeedSequence([self.seed, self._key(name)])
            self._streams[name] = np.random.Generator(np.random.PCG64(ss))
        return self._streams[name]

    def faker(self, name: str) -> Faker:
        """en_US Faker instance (cached per name) seeded deterministically from (seed, name)."""
        if name not in self._fakers:
            ss = np.random.SeedSequence([self.seed, self._key(name), 1])
            fk = Faker("en_US")
            fk.seed_instance(int(ss.generate_state(1, dtype=np.uint32)[0]))
            self._fakers[name] = fk
        return self._fakers[name]

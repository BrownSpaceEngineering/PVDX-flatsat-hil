"""Radios for the UHF ground station: the HackRF (half duplex, through hackrf_transfer), and an in-memory pair for
testing the ground's protocol code without hardware.

A radio sends PSDUs (the satellite's USLP frames; the modem appends the FCS) and delivers received mrfsk.Received
frames on a queue. The HackRF decodes coded frames with the satellite's code (mrfsk.check_phy()) and sends uncoded
frames unless given tx_code, since the satellite receives both. The HackRF can't receive while it transmits: send() stops the receiver, transmits, and restarts it,
which takes a few hundred milliseconds of USB setup each way. PUSP's retry period (UHF_RETRY_MS) has to cover that;
the turnaround is measured and reported so it can be checked against it on the bench.

Use attenuators on a cabled link: never connect the HackRF straight to the flatsat's antenna port.
"""

import queue
import shutil
import subprocess
import threading
import time

import numpy as np

import mrfsk

READ_BYTES = 262144  # hackrf_transfer output read at a time (65536 samples, 33 ms)


class Radio:
    def send(self, psdu):
        raise NotImplementedError

    def receive(self, timeout):
        """The next received frame, or None after timeout seconds."""
        try:
            return self.rx.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        pass


class HackRF(Radio):
    def __init__(self, freq_hz, lna_db=24, vga_db=20, tx_vga_db=0, serial=None, code=mrfsk.RSC, interleaved=False,
                 tx_code=None):
        if shutil.which("hackrf_transfer") is None:
            raise FileNotFoundError("hackrf_transfer not found: install the HackRF tools (brew install hackrf)")
        self.freq_hz = freq_hz
        self.code = code  # What coded frames are decoded with
        self.interleaved = interleaved
        self.tx_code = tx_code  # What the ground codes its frames with, or None to send them uncoded
        self.rx_gains = ["-l", str(lna_db), "-g", str(vga_db)]
        self.tx_gain = ["-x", str(tx_vga_db)]
        self.device = ["-d", serial] if serial else []
        self.rx = queue.Queue()
        self.lock = threading.Lock()
        self.proc = None
        self.reader = None
        self.turnarounds = []  # Seconds from asking to send until the receiver ran again
        self._start_rx()

    def _common(self):
        return self.device + ["-f", str(self.freq_hz), "-s", str(mrfsk.SAMPLE_RATE)]

    def _start_rx(self):
        demod = mrfsk.Demodulator(code=self.code, interleaved=self.interleaved)
        proc = subprocess.Popen(["hackrf_transfer", "-r", "-"] + self._common() + self.rx_gains, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)

        def read():
            while True:
                data = proc.stdout.read(READ_BYTES)
                if not data:
                    return
                for frame in demod.feed(mrfsk.from_hackrf(data)):
                    frame.received_at = time.time()
                    self.rx.put(frame)

        self.proc = proc
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def _stop_rx(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            self.reader.join(timeout=5)
            self.proc = None

    def send(self, psdu):
        iq = mrfsk.modulate(mrfsk.frame_bits(psdu, self.tx_code, self.interleaved))
        tail = np.zeros(mrfsk.SPS * 32, dtype=complex)  # Let the last symbols leave the DAC before the stream ends
        data = mrfsk.to_hackrf(np.concatenate([iq, tail]))
        with self.lock:
            t0 = time.monotonic()
            self._stop_rx()
            subprocess.run(["hackrf_transfer", "-t", "-", "-a", "0"] + self._common() + self.tx_gain, input=data,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            self._start_rx()
            self.turnarounds.append(time.monotonic() - t0)

    def close(self):
        with self.lock:
            self._stop_rx()


class LoopbackRadio(Radio):
    """One end of an in-memory link: what one end sends, the other receives (no modem, no losses unless drop is set)."""

    def __init__(self):
        self.rx = queue.Queue()
        self.peer = None
        self.drop = None  # drop(psdu) -> True to lose a frame
        self.sent = []

    @classmethod
    def pair(cls):
        a, b = cls(), cls()
        a.peer, b.peer = b, a
        return a, b

    def send(self, psdu):
        self.sent.append(bytes(psdu))
        if self.drop and self.drop(psdu):
            return
        frame = mrfsk.Received(bytes(psdu) + b"\0\0", True, False, 0, -20.0, 0.0)
        frame.received_at = time.time()
        self.peer.rx.put(frame)

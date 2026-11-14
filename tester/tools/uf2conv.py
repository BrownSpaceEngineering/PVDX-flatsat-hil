# Converts a flat binary into a UF2 file for the SAMD51 UF2 bootloader.
# Usage: python3 uf2conv.py <input.bin> <load address> <output.uf2>
#
# Format: https://github.com/microsoft/uf2 (512-byte blocks, 256 bytes of payload each, family ID flag set)

import struct
import sys

UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_FAMILY_ID = 0x00002000
SAMD51_FAMILY_ID = 0x55114460
PAYLOAD_SIZE = 256


def convert(data, address):
    blocks = [data[i:i + PAYLOAD_SIZE] for i in range(0, len(data), PAYLOAD_SIZE)]
    out = bytearray()
    for n, chunk in enumerate(blocks):
        header = struct.pack("<8I", UF2_MAGIC_START0, UF2_MAGIC_START1, UF2_FLAG_FAMILY_ID, address + n * PAYLOAD_SIZE,
                             PAYLOAD_SIZE, n, len(blocks), SAMD51_FAMILY_ID)
        payload = chunk.ljust(476, b"\x00")
        out += header + payload + struct.pack("<I", UF2_MAGIC_END)
    return bytes(out)


def main():
    if len(sys.argv) != 4:
        print(__doc__ or "usage: uf2conv.py <input.bin> <load address> <output.uf2>")
        return 1
    with open(sys.argv[1], "rb") as f:
        data = f.read()
    with open(sys.argv[3], "wb") as f:
        f.write(convert(data, int(sys.argv[2], 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

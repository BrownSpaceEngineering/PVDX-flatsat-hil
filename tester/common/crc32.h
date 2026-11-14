#ifndef CRC32_H
#define CRC32_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// crc32_update(0, data, len) equals Python's zlib.crc32(data); pass the previous result to continue
uint32_t crc32_update(uint32_t crc, const uint8_t *data, size_t len);

#ifdef __cplusplus
}
#endif

#endif // CRC32_H

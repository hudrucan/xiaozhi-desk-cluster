#include <stddef.h>
#include <stdint.h>
/* Native packing keeps ARMv6 CPU free of per-pixel Python loops. */
void desk_rgb565(const uint8_t *rgb, uint8_t *output, size_t pixels) {
    for (size_t i = 0; i < pixels; ++i) {
        uint16_t value = ((uint16_t)(rgb[i*3] >> 3) << 11)
                       | ((uint16_t)(rgb[i*3+1] >> 2) << 5)
                       | (rgb[i*3+2] >> 3);
        output[i*2] = value & 255;
        output[i*2+1] = value >> 8;
    }
}

#ifndef VW_HOST_PLATFORM_H
#define VW_HOST_PLATFORM_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void vw_host_input_lock(void);
void vw_host_input_unlock(void);
uint64_t vw_host_now_us(void);
void vw_host_sleep_ms(unsigned milliseconds);

#ifdef __cplusplus
}
#endif

#endif

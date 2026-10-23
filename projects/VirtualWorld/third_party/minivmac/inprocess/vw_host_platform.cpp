#include "vw_host_platform.h"

#include <chrono>
#include <mutex>
#include <thread>

namespace {
std::mutex input_mutex;
}

extern "C" void vw_host_input_lock(void)
{
    input_mutex.lock();
}

extern "C" void vw_host_input_unlock(void)
{
    input_mutex.unlock();
}

extern "C" uint64_t vw_host_now_us(void)
{
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

extern "C" void vw_host_sleep_ms(unsigned milliseconds)
{
    std::this_thread::sleep_for(std::chrono::milliseconds(milliseconds));
}

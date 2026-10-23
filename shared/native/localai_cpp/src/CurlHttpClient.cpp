#include "LocalAI.hpp"

#ifdef LOCALAI_USE_LIBCURL

#include <curl/curl.h>
#include <stdexcept>

namespace localai {

static size_t curlWrite(void* ptr, size_t size, size_t nmemb, void* userdata) {
    std::string* out = static_cast<std::string*>(userdata);
    size_t total = size * nmemb;
    out->append(static_cast<char*>(ptr), total);
    return total;
}

class CurlHttpClientImpl : public IHttpClient {
public:
    HttpResponse send(const HttpRequest& request) override {
        CURL* curl = curl_easy_init();
        if (!curl) throw std::runtime_error("curl_init_failed");
        struct CurlHolder { CURL* c; CurlHolder(CURL* c):c(c){} ~CurlHolder(){ if(c) curl_easy_cleanup(c); } } holder(curl);

        std::string response;
        curl_easy_setopt(curl, CURLOPT_URL, request.url.c_str());
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, static_cast<long>(request.timeoutSeconds));
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, &curlWrite);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response);

        struct curl_slist* headers = nullptr;
        for (const auto& h : request.headers) {
            std::string line = h.first + ": " + h.second;
            headers = curl_slist_append(headers, line.c_str());
        }
        if (headers) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);

        if (request.method == "POST") {
            curl_easy_setopt(curl, CURLOPT_POST, 1L);
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, request.body.c_str());
            curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)request.body.size());
        }

        CURLcode code = curl_easy_perform(curl);
        long status = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);

        if (headers) curl_slist_free_all(headers);

        HttpResponse resp;
        resp.statusCode = static_cast<long>(status);
        resp.body = response;
        if (code != CURLE_OK) {
            throw std::runtime_error(std::string("curl_error:") + curl_easy_strerror(code));
        }
        return resp;
    }
};

std::shared_ptr<IHttpClient> makeCurlHttpClient() {
    return std::make_shared<CurlHttpClientImpl>();
}

} // namespace localai

#endif // LOCALAI_USE_LIBCURL

"""Distribution policy for application-provided model downloads only.

No request is made during import or local inference. Region lookup uses the
caller's public IP through Country.is; only a country code is retained.
"""

import json
import re
from urllib.request import Request, urlopen


COUNTRY_LOOKUP_URL = "https://api.country.is/"


class ModelDownloadUnavailable(PermissionError):
    pass


def require_model_download_available():
    try:
        request = Request(COUNTRY_LOOKUP_URL, headers={"User-Agent": "LocalAI-model-download"})
        with urlopen(request, timeout=8) as response:
            data = json.loads(response.read(16384))
        country = data.get("country") if isinstance(data, dict) else None
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country) or country in {"XX", "ZZ"}:
            raise ValueError("Country lookup returned no valid country code")
    except Exception as exc:
        raise ModelDownloadUnavailable(
            "暂时无法确认下载地区，模型下载已暂停；请稍后重试或导入自备模型。\n"
            "Unable to verify the download region. Retry later or import your own model."
        ) from exc
    if country == "CN":
        raise ModelDownloadUnavailable(
            "因备案安排，暂不向中国大陆地区提供模型下载。您仍可导入并运行自备模型。\n"
            "Model downloads are temporarily unavailable in mainland China. "
            "You can still import and run your own models."
        )

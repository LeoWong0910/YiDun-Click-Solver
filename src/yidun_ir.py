"""YiDun IR upload payload helpers.

The final solver must not depend on browser runtime state.  The IR endpoint only
needs a plausible encrypted sensor blob to mint an ``irToken`` for /api/v3/get.
``TEMPLATE_SENSOR_D`` is a sanitized browser-like sensor blob captured from the
challenge SDK and replayed with a fresh nonce; it is not a cookie or account
secret.  ``empty`` mode is kept as a fallback/boundary probe.
"""

from __future__ import annotations

import secrets
from typing import Literal

from src.yidun_crypto import aes_encrypt


IRMode = Literal["template", "empty"]

TEMPLATE_SENSOR_D = (
    'zCr1Q+ns.l9l6EkqiBrnIyszJu8J+nVR/xVFIz0AqTEgX+WhD/xstVjoqX+VZXu8ExQhC8tZx49yx48LzbJSHzcG0b30KpJavyES9RvuTbDdaj4LDqUBluA0IpTc4qLowuLYmmjsEHgj41GSXS9iStn8q145Uat5oVbTn1NUrTy/.E+qryUmN6NCNjmeKm/r+VpOVASatzIGYFyVAAgKQGT80NZsvT.+9zuaJBpp.ZaQwOLNTokFoqQuz0gyL9bNm.xWlS8thjNCHCfzpi+5s6YryHwsnANjm8DZNZ5A.MgBSPM.fjiOqm5/oVhN.vE4Yu/whENTOnuBE0+2kc4mQV4YlqUZWV.ekOhkTqRnfMqEvnlZxT6yd2ko03ZCVQ0iN0cYrwVGSurQMGbzjSXyygqfmdC9pjzH+aKl8BS4SUd9cEOnNZNsnGynNErTkPlP2SlkhNAmr9q0YRQMaunzNKMblawvfowK6mzHyAhkf4WNWYdsTDMp2HceyjDYRv0jBy0gccMpDl.emgaPViutBHtlliEIsPg+oMdrqMHrDliCpOBrb.535DSnK3qA5SdqTfyEcBETN6tOy0m3Xjr8rPC4KSVZVolpwRfaRxpkK.yeaFlZ5dX2RQqWI4Z9Le.6Asymdmv0Uoq9Sd.OksHjinzR0VDL4HNYcjjcQZTlV3vxrLn/I9oCmOKsN5hrpa4ESkzphLxuyRxAwLiOCbk1j0xeB1Q1V18X8tjM3jrRzocZQQdEfcJnVDsZg9NMfdZKv3LyF.wGeoip8FqssW80XbtWGCWJ4xY/rsmVoS1nhXk+LuF0hPrL9de0CR0XtE94rUhDIAoZ.fDYGGeYMWQrtMibG121OSa1bYLovskyvv6aaBRpWfx5LcQHtD4202ncpJqD+P+0NS.u3tFWkZwwFIKBlfbcU/ZaWAWPGJgBonEZZPlCbOYuTwJh6OoWC8hrK4RItrQ/IPe/syHIKaLUKRBwCJcuJDBsn1rwS.pO4myS1lQrS1CinfYqdcdBKGV4.Aj990WA9rlgqcPmaI4xsMjc2FpkR1wFQVEvPtQQhKR6UfA/iov0sBysjsiE4lzToO2i+IsTmehu0jwzOd1zdDgjNUnGkJHQbq05tAypT1gtLXIGoe6nq89WKfgBJOS9yTr0P.Fmq+93VR1jkkCqd1kFSlFy56lP2bmZzWx/rGvq9.HN.pupvIUPUu.oEnkIfNjG8MjyKpubT3KJef6x4CBkFiaM0Adoh9kOflIV9x5rnGcS0jdiW6YQ/R8SxY/bC3jmIR9vQnYdeCPAxbYl25X1SNkCyLcnctXtslY/lHKx01tET65tc4F3OPq8zgSXLUzGOy1KBA4T52X84gZziGdIz/Ip2lfpO4uMnYc/KJElR+o6p+M2hMwCHmKUuHXPf.yKdcdXb4neGf3j1ihbLCwFORXUIHtkIRx2kR1brGeemwQoGVprJZhgczB2He+rZPFlVYlvyRwHexiOfChejAVblSOaKaqDyUWbODJ2VaNNIh3h1hnqpCPnrvISxl4SYq2ncM77'
)


def build_ir_body(pn: str, sdk_version: str, version_key: str, *, mode: IRMode = "template") -> dict[str, str]:
    if mode == "template":
        sensor_d = TEMPLATE_SENSOR_D
    elif mode == "empty":
        sensor_d = aes_encrypt("")
    else:
        raise ValueError(f"unsupported IR mode: {mode}")
    return {
        "p": pn,
        "v": sdk_version,
        "vk": version_key,
        "n": secrets.token_hex(16),
        "d": sensor_d,
    }


__all__ = ["IRMode", "TEMPLATE_SENSOR_D", "build_ir_body"]

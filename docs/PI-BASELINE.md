# Pi baseline

Reported by Eli on 2026-10-10:

```
kiwix-tools 3.7.0
libkiwix 14.0.0
libzim 9.2.3
libxapian 1.4.25
libcurl 8.12.1
libmicrohttpd 1.0.1
libz 1.3.1
libicu 76.1.0
libpugixml 1.14.0
libzstd 1.5.6
liblzma 5.6.4
```

Kiwix listening port and ZIM/book names: **pending**. No default value in tests is
a measurement of this Pi. Capture `ps -ww -C kiwix-serve -o args=` and
`ss -ltnp | grep -i kiwix`. A real XML search and article recording are also pending.

The remaining Phase 0 system measurements and Ollama details have not been recorded.

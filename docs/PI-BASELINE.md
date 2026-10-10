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

Eli reports that `ps -ww -C kiwix-serve -o args=` and `ss -ltnp | grep -i kiwix`
returned no output: no running kiwix-serve process was found at capture time.

ZIM path reported by Eli:

```
/home/eli/kiwix/zims/restarters_en_all_maxi_2026-04.zim
```

This is the Restarters archive; no Wikipedia archive has been reported yet. The
sonobuoy Wikipedia acceptance check needs an appropriate Wikipedia ZIM.

Kiwix listening port and confirmed book key: **pending**. A foreground test on
`127.0.0.1:8080` with `books.name=restarters_en_all_maxi_2026-04` has been suggested;
it is not yet a verified running configuration. A real XML search and article recording
are also pending. No default value in synthetic tests is a measurement of this Pi.

The remaining Phase 0 system measurements and Ollama details have not been recorded.

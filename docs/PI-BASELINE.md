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

Eli then started a foreground test at `127.0.0.1:8080` and supplied the XML search
response. Confirmed test settings: port **8080**, book key
**restarters_en_all_maxi_2026-04**, parameter **books.name**. Query `repair` returned
129 total hits; the first URL is
`/content/restarters_en_all_maxi_2026-04/Glossary%3ARepair_Caf%C3%A9`.
The response is recorded in `tests/fixtures/kiwix-3.7.0-restarters.xml`.
Eli also fetched that article URL successfully; its HTML is recorded in
`tests/fixtures/kiwix-3.7.0-repair-cafe.html`. Both recordings pass parsing/extraction
tests without network access. The Wikipedia acceptance check still needs a Wikipedia
archive. This was a foreground test; persistence across reboot is not established.

## Memory and Ollama

Reported by Eli on 2026-10-11:

```
$ free -h
               total        used        free      shared  buff/cache   available
Mem:           3.7Gi       661Mi       1.8Gi        68Mi       1.4Gi       3.1Gi
Swap:          2.0Gi          0B       2.0Gi
$ ollama --version
ollama version is 0.34.4
$ ollama list
NAME          ID              SIZE      MODIFIED
qwen3:0.6b    7df6b6e09427    522 MB    13 days ago
```

`qwen3:0.6b` is the only model and matches the librarian default. This Ollama version
accepts `think: false`. With 3.1 GiB available, the 522 MB model and a 2,048-token context
fit without swapping. The first `python3 -m core.librarian.ollama` reply took about
17.6 s to its first word (probably including the model load).

Still not recorded: `uname -m`, `df -h`, `vcgencmd measure_temp`, `vcgencmd get_throttled`
and `lsblk`.

#!/usr/bin/env python3
import argparse, os, sys, urllib.request, time

URL="https://www.geodatenportal.sachsen-anhalt.de/gfds_webshare/download/LVermGeo/Geodatenportal/externedaten/GBIS_Flurstuecke.zip"

def main():
    ap=argparse.ArgumentParser(description="Amtliche ALKIS-Flurstücke Sachsen-Anhalt herunterladen.")
    ap.add_argument("output", help="Zieldatei GBIS_Flurstuecke.zip")
    a=ap.parse_args()
    out=os.path.abspath(a.output)
    os.makedirs(os.path.dirname(out) or ".",exist_ok=True)
    tmp=out+".part"
    req=urllib.request.Request(URL,headers={"User-Agent":"RTK-Flurstuecksfinder/3.0"})
    print("Quelle:",URL)
    print("Ziel:  ",out)
    with urllib.request.urlopen(req,timeout=60) as r, open(tmp,"wb") as f:
        total=int(r.headers.get("Content-Length") or 0)
        got=0;last=0
        while True:
            b=r.read(1024*1024)
            if not b:break
            f.write(b);got+=len(b)
            now=time.time()
            if now-last>1:
                if total: print(f"\r{got/1024/1024:.0f} / {total/1024/1024:.0f} MB ({got*100/total:.1f}%)",end="",flush=True)
                else: print(f"\r{got/1024/1024:.0f} MB",end="",flush=True)
                last=now
    print()
    if os.path.getsize(tmp)<1024*1024:
        raise RuntimeError("Download unerwartet klein.")
    with open(tmp,"rb") as f:
        if f.read(2)!=b"PK":raise RuntimeError("Download ist keine ZIP-Datei.")
    os.replace(tmp,out)
    print("FERTIG:",out)

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("FEHLER:",e,file=sys.stderr);sys.exit(1)

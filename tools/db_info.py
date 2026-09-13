#!/usr/bin/env python3
import sqlite3,sys,os
p=sys.argv[1] if len(sys.argv)>1 else 'alkis_st.sqlite'
c=sqlite3.connect(p)
print('Datei:',p, f'{os.path.getsize(p)/1024/1024:.1f} MB')
print('Flurstuecke:',c.execute('select count(*) from parcels').fetchone()[0])
print('\nGebiete:')
for r in c.execute('select kreisschl,kreis,count(*) from parcels group by kreisschl,kreis order by kreis'):
 print(f'  {r[0]}  {r[1]}: {r[2]:,}')
print('\nQuellen:')
for r in c.execute('select filename,feature_count,imported_at from sources order by filename'):print(' ',r)

// Run with node tests/test_height_ui.cjs. Pure UI logic with small DOM/map doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '..', 'app.py'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0]
  .replace('setInterval(poll,1000);poll();', '')
  .replace('loadHeights();loadCounties();', '');
const element = () => ({textContent:'', value:'', innerHTML:'', className:'', style:{}, checked:true,
  classList:{toggle(){},add(){},remove(){}},appendChild(){}});
const ctx = {console, setTimeout, clearTimeout, setInterval, clearInterval,
  Option: function(text,value){this.text=text;this.value=value},
  document:{querySelectorAll:()=>[{}, {}, {}],createElement:element}};
for (const [, id] of html.matchAll(/id="([\w-]+)"/g)) ctx[id]=element();
for (const id of ['htol','hduration']) {
 const e=ctx[id]; e.options=[]; e.add=o=>e.options.push(o);
 Object.defineProperty(e,'value',{get(){return this.selected||''},set(v){this.selected=this.options.some(o=>o.value===v)?v:''}});
}
vm.createContext(ctx);
vm.runInContext(script,ctx);
const run = code => vm.runInContext(code,ctx);
(async()=>{
  // Zero is a valid pole height, not a request for the 2m default.
  run("heightData={settings:{mode:'MSL',pole_height_m:0,target_msl_m:102,tolerance_mm:20}};updateHeightLive({alt:102,fix:'RTK FIX',age:0})");
  assert.equal(ctx.liveheight.textContent,'102.000 m');
  assert.equal(ctx.hlivedelta.textContent,'0 mm');
  run("updateHeightLive({alt:102,fix:'RTK FIX',age:10})");
  assert.equal(ctx.hlivequality.textContent,'NICHT MESSEN');
  // Legacy nonstandard settings remain selected when their options are absent.
  ctx.fetch=async()=>({json:async()=>({settings:{mode:'MSL',pole_height_m:0,tolerance_mm:25,duration_s:60},points:[]})});
  await run('loadHeights()');
  assert.equal(ctx.htol.value,'25'); assert.equal(ctx.hduration.value,'60');
  assert.equal(ctx.hpole.value,'0.000');
  // A slow first map load must finish before centering/opening the point.
  ctx.readyResolve=null;ctx.mapCalls=[];
  run("heightData={points:[{id:1,lon:11.48,lat:51.77}]};tab=()=>{};initMap=()=>new Promise(r=>readyResolve=r);showHeightPopup=id=>mapCalls.push('popup');applyMapFilters=()=>{};map={flyTo:()=>mapCalls.push('fly')}");
  const pending=run('showHeightOnMap(1)');
  assert.deepEqual(ctx.mapCalls,[]);
  ctx.readyResolve();await pending;
  assert.deepEqual(ctx.mapCalls,['fly','popup']);
  // Repeated button taps while measuring must not issue another request.
  ctx.fetch=()=>{throw new Error('duplicate capture request')};
  run('heightMeasuring=true');await run("measureHeight('reference')");
  console.log('PASS: zero pole, stale fix, legacy settings, delayed map, duplicate capture');
})().catch(e=>{console.error(e);process.exitCode=1});

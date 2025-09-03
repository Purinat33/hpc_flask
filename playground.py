# --- Playground UI (unchanged) ---
PAGE = """
<!doctype html><meta charset="utf-8"><title>HPC Cost Playground</title>
<style>body{font-family:system-ui,Arial,sans-serif;margin:2rem}.card{max-width:760px;padding:1rem 1.25rem;border:1px solid #ddd;border-radius:12px}.row{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin-bottom:.75rem}label{display:block;font-weight:600;margin-bottom:.25rem}input,select{width:100%;padding:.6rem;border:1px solid #bbb;border-radius:8px}.muted{color:#666;font-size:.9rem}.total{font-size:1.25rem;font-weight:800;margin-top:1rem}.breakdown{margin-top:.5rem;color:#444}.pill{display:inline-block;margin-right:.5rem;margin-top:.35rem;padding:.25rem .5rem;border-radius:999px;background:#f3f4f6}</style>
{{ NAV|safe }}
<h2>HPC Cost Playground</h2>
<p class="muted">Rates come from <code>/formula?type=...</code>. Use the Admin page to edit them.</p>
<div class="card">
  <div class="row">
    <div><label>Customer type</label>
      <select id="tier">
        <option value="mu">MU</option><option value="gov">Gov</option><option value="private">Private</option>
      </select></div>
    <div><label>Duration amount</label><input id="amount" type="number" min="1" step="1" value="1"></div>
  </div>
  <div class="row">
    <div><label>Duration unit</label>
      <select id="unit"><option value="hour">Hour(s)</option><option value="day">Day(s)</option><option value="month">Month(s)</option></select></div>
    <div><label>CPU cores</label><input id="cpu" type="number" min="0" step="1" value="1"></div>
  </div>
  <div class="row">
    <div><label>GPU count</label><input id="gpu" type="number" min="0" step="1" value="0"></div>
    <div><label>Memory (GB)</label><input id="mem" type="number" min="0" step="1" value="1"></div>
  </div>
  <div class="muted">
    <span class="pill" id="showCpuRate">cpu: —</span>
    <span class="pill" id="showGpuRate">gpu: —</span>
    <span class="pill" id="showMemRate">mem: —</span>
    <span class="pill" id="showUnit">unit: per-hour</span>
    <a class="pill" href="/admin">🔐 admin</a>
  </div>
  <div class="total" id="total">Total: ฿0.00</div>
  <div class="breakdown" id="breakdown"></div>
  <div id="error" style="color:#b00020;margin-top:.5rem"></div>
</div>
<script>
const $=id=>document.getElementById(id);
const tierEl=$('tier'), amountEl=$('amount'), unitEl=$('unit'), cpuEl=$('cpu'), gpuEl=$('gpu'), memEl=$('mem');
let rates={cpu:0,gpu:0,mem:0};
function hoursFrom(n,u){n=Number(n)||0;return u==='day'?n*24:u==='month'?n*720:n;}
async function fetchRates(){
  $('error').textContent="";
  try{
    const t=tierEl.value;
    const res=await fetch('/formula?type='+encodeURIComponent(t));
    if(!res.ok) throw new Error('HTTP '+res.status);
    const d=await res.json(); rates=d.rates||{cpu:0,gpu:0,mem:0};
    $('showCpuRate').textContent=`cpu: ฿${rates.cpu}/cpu-hour`;
    $('showGpuRate').textContent=`gpu: ฿${rates.gpu}/gpu-hour`;
    $('showMemRate').textContent=`mem: ฿${rates.mem}/GB-hour`;
    $('showUnit').textContent='unit: per-hour';
    recalc();
  }catch(e){ $('error').textContent='Failed to fetch rates: '+e; rates={cpu:0,gpu:0,mem:0}; recalc(); }
}
function recalc(){
  const hrs=hoursFrom(amountEl.value, unitEl.value);
  const cpuCost=(Number(cpuEl.value)||0)*hrs*rates.cpu;
  const gpuCost=(Number(gpuEl.value)||0)*hrs*rates.gpu;
  const memCost=(Number(memEl.value)||0)*hrs*rates.mem;
  const total=cpuCost+gpuCost+memCost;
  $('total').textContent=`Total: ฿${total.toFixed(2)}`;
  $('breakdown').textContent=`CPU: ฿${cpuCost.toFixed(2)} | GPU: ฿${gpuCost.toFixed(2)} | MEM: ฿${memCost.toFixed(2)} (duration: ${hrs} h)`;
}
[tierEl, amountEl, unitEl, cpuEl, gpuEl, memEl].forEach(el=>{
  el.addEventListener('input', ()=>{ if(el===tierEl) fetchRates(); else recalc(); });
  el.addEventListener('change', ()=>{ if(el===tierEl) fetchRates(); else recalc(); });
});
fetchRates();
</script>
"""


def return_playground():
    return PAGE

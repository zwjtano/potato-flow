(function(){
  const key='potatoflow-sidebar-collapsed'; const button=document.getElementById('sidebarCollapse');
  const savedPreference=()=>localStorage.getItem(key)==='1';
  function apply(collapsed,persist=true){document.body.classList.toggle('sidebar-collapsed',collapsed);button?.setAttribute('aria-label',collapsed?'展开侧边导航':'折叠侧边导航');if(persist)localStorage.setItem(key,collapsed?'1':'0')}
  const compact=window.matchMedia('(max-width: 1320px)');
  apply(compact.matches||savedPreference(),false);
  button?.addEventListener('click',()=>apply(!document.body.classList.contains('sidebar-collapsed')));
  compact.addEventListener?.('change',event=>apply(event.matches||savedPreference(),false));
  async function refreshStatus(){try{const response=await fetch('/live-recording/status',{headers:{'X-Requested-With':'XMLHttpRequest'}});if(!response.ok)return;const data=await response.json();const active=(data.rooms||[]).filter(room=>room.recording).length;const runtime=document.getElementById('topbarRuntime');runtime?.classList.toggle('running',Boolean(data.running));if(runtime)runtime.querySelector('span').textContent=data.running?'录制核心运行中':'录制核心已停止';const count=document.getElementById('topbarRecording');if(count)count.textContent='录制 '+active;const disk=document.getElementById('topbarDisk');if(disk)disk.textContent='磁盘 '+(data.recordings_free_text||'--')}catch(error){const runtime=document.getElementById('topbarRuntime');if(runtime)runtime.querySelector('span').textContent='录制核心状态不可用'}}
  refreshStatus(); window.setInterval(refreshStatus,5000);
}());

// Session credentials stay HttpOnly; this per-session CSRF value is fetched, not embedded.
(() => {
 const nativeFetch=window.fetch.bind(window);
 const ready=nativeFetch('/auth/csrf',{credentials:'same-origin',cache:'no-store'}).then(async response=>{
  if(response.status===403){location.replace('/');throw Error('Owner session expired')}
  if(!response.ok)throw Error('Owner session temporarily unavailable');
  return response.json();
 });
 window.fetch=async(input,init={})=>{
  const url=new URL(input instanceof Request?input.url:String(input),location.href);
  if(url.origin!==location.origin)throw Error('Only same-origin owner requests allowed');
  const session=await ready;
  const headers=new Headers(init.headers||(input instanceof Request?input.headers:undefined));
  headers.delete('x-review-token');headers.set('x-owner-csrf',session.csrf);
  return nativeFetch(input,{...init,headers,credentials:'same-origin'});
 };
 const showWebOnly=()=>{
  const logout=document.createElement('button');logout.id='owner-logout';logout.type='button';logout.textContent='Выйти';
  logout.onclick=async()=>{const response=await fetch('/auth/logout',{method:'POST'});if(response.ok)location.replace('/')};
  (document.querySelector('.sidebar')||document.body).append(logout);
  if(document.documentElement.dataset.webOnly!=='true')return;
  const banner=document.createElement('p');banner.id='owner-web-only';banner.role='status';
  banner.textContent='Web-only: банк, импорт, Telegram, ML и финансовые изменения отключены. Облачный чат — только после отдельного согласия.';
  document.body.prepend(banner);
  const reads=new Set(['cfo-question','cfo-history-filters','cfo-search']);
  const disableWrites=()=>{
   for(const form of document.forms)if(!reads.has(form.id)&&!form.closest('#cognitive-chat'))for(const field of form.elements)field.disabled=true;
   for(const button of document.querySelectorAll('button'))if(!button.closest('#cognitive-chat')&&(['cfo-refresh','cfo-explain','cfo-import-apply'].includes(button.id)||/^(Предложить категорию|Подтвердить|Применить|Обучить|Продвинуть|Отозвать)/.test(button.textContent.trim())))button.disabled=true;
   for(const field of document.querySelectorAll('input[type=file]'))field.disabled=true;
  };
  disableWrites();new MutationObserver(disableWrites).observe(document.body,{childList:true,subtree:true});
 };
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',showWebOnly);else showWebOnly();
})();

'use strict';
const $ = id => document.getElementById(id);
const labels = {received:'Получено',sending:'Отправляется',published:'Опубликовано',rejected:'Отклонено',warning:'Предупреждение',banned:'Блокировка',command:'Команда',profile:'Настройка ника',failed:'Ошибка',unknown:'Не подтверждено',partial:'Частично'};
const mediaNames = {text:'Текст',photo:'Фото',video:'Видео',animation:'Анимация',audio:'Аудио',voice:'Голосовое',document:'Документ',sticker:'Стикер',video_note:'Видеокружок',contact:'Контакт',location:'Геопозиция',venue:'Место',poll:'Опрос',dice:'Кубик',other:'Другое'};
const reasonLabels = {cooldown:'Пауза между публикациями',spam_cooldown:'Пауза после предупреждения',warning:'Предупреждение за спам',banned:'Действует блокировка',new_ban:'Повторный спам — блокировка на час'};
let csrf='', page=1, nextNumber='…', request=null, loading=false, loadAgain=false, publishing=false, channel='', listVersion=0;
const dates = new Intl.DateTimeFormat('ru-RU',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
function node(tag, cls, text) {const el=document.createElement(tag);if(cls) el.className=cls;if(text!==undefined) el.textContent=text;return el;}
function badge(status){return node('span','badge '+status,labels[status]||status);}
function showError(message){$('global-error').textContent=message;$('global-error').hidden=!message;}
function showLogin(){csrf='';$('login-screen').hidden=false;$('app').hidden=true;$('detail-dialog').close();}
async function api(path, options={}){
  const response=await fetch(path,{...options,credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  const data=await response.json();
  if(!response.ok){if(response.status===401&&path!='/api/login')showLogin();throw new Error(data.error||'Не удалось выполнить запрос.');}
  return data;
}
function enter(data){csrf=data.csrf;channel=data.channel;$('login-screen').hidden=true;$('app').hidden=false;$('password').value='';$('channel-link').textContent=channel;if(channel.startsWith('@'))$('channel-link').href='https://t.me/'+encodeURIComponent(channel.slice(1));$('preview-channel').textContent=channel;load();}
$('login-form').addEventListener('submit',async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;$('login-error').textContent='';try{enter(await api('/api/login',{method:'POST',body:JSON.stringify({password:$('password').value})}));}catch(error){$('login-error').textContent=error.message;}finally{button.disabled=false;}});
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST',body:'{}'});showLogin();}catch(error){showError(error.message);}});
function preview(){const text=$('post-text').value;$('char-count').textContent=`${text.length} / 4000`;$('preview-body').textContent=text||'Текст вашего сообщения…';$('preview-body').classList.toggle('placeholder',!text);$('preview-footer').textContent=`№${nextNumber} — ${$('role').value}`;}
$('post-text').addEventListener('input',preview);$('role').addEventListener('change',preview);
$('compose-nav').addEventListener('click',()=>{$('composer').scrollIntoView({behavior:'smooth',block:'start'});$('post-text').focus({preventScroll:true});});
function renderRows(items){const fragment=document.createDocumentFragment();for(const message of items){const row=node('tr','message-row');row.tabIndex=0;row.setAttribute('aria-label',`Открыть сообщение ${message.post_number??message.id}`);row.addEventListener('click',()=>detail(message.id));row.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();detail(message.id);}});
  const number=node('td');number.append(node('span','cell-title',message.post_number?'№'+message.post_number:'—'),node('span','cell-meta',dates.format(new Date(message.sent_at*1000))));
  const content=node('td');content.append(node('span','message-summary',message.body||`[${mediaNames[message.media_type]||'Вложение'}]`),node('span','cell-meta',(message.origin==='dashboard'?'Из панели · ':'')+(mediaNames[message.media_type]||'Вложение')));
  const sender=node('td');sender.append(node('span','cell-title',message.sender_name||message.nickname||'Без имени'),node('span','cell-meta',message.origin==='dashboard'?'Администрация':(message.sender_username?'@'+message.sender_username+' · ':'')+'ID '+message.sender_id));
  const status=node('td');status.append(badge(message.status));row.append(number,content,sender,status);fragment.append(row);}
  $('messages').replaceChildren(fragment);
}
async function load(){if(!csrf)return;if(loading){loadAgain=true;return;}loading=true;const version=++listVersion;try{const search=$('search').value;const status=$('status').value;const data=await api('/api/messages?'+new URLSearchParams({search,status,page}));if(!csrf||version!==listVersion)return;renderRows(data.items);$('empty').hidden=data.items.length>0;const filtered=Boolean(search||status);$('empty').querySelector('h3').textContent=filtered?'Ничего не найдено':'Здесь появятся сообщения';$('empty').querySelector('p').textContent=filtered?'Измените поисковый запрос или выбранный статус.':'Запустите обновлённого бота и отправьте ему сообщение. Журнал сохраняет историю с момента подключения.';
  for(const key of ['total','published','senders','rejected'])$('stat-'+key).textContent=new Intl.NumberFormat('ru-RU').format(data.stats[key]);
  $('bot-state').textContent=data.bot_online?'● Бот подключён':'Нет сигнала от бота';$('bot-state').classList.toggle('online',data.bot_online);
  $('result-count').textContent=`${data.total?((data.page-1)*data.page_size+1):0}–${Math.min(data.page*data.page_size,data.total)} из ${data.total}`;$('page-label').textContent=data.page;$('prev').disabled=data.page<=1;$('next').disabled=data.page*data.page_size>=data.total;nextNumber=data.next_number;preview();showError('');
}catch(error){showError(error.message);}finally{loading=false;if(loadAgain){loadAgain=false;load();}}}
let searchTimer;$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;load();},250);});$('status').addEventListener('change',()=>{page=1;load();});$('refresh').addEventListener('click',load);$('prev').addEventListener('click',()=>{page=Math.max(1,page-1);load();});$('next').addEventListener('click',()=>{page++;load();});
function feedback(text,error=false){$('publish-feedback').hidden=false;$('publish-feedback').textContent=text;$('publish-feedback').classList.toggle('error',error);}
$('publish-form').addEventListener('submit',async event=>{event.preventDefault();if(publishing)return;const text=$('post-text').value.trim(),role=$('role').value;if(!text)return;
  // Keep the same request ID after a network failure: retrying must never post twice.
  if(!request||request.text!==text||request.role!==role)request={text,role,request_id:crypto.randomUUID()};
  publishing=true;$('publish-button').disabled=true;$('post-text').disabled=true;$('role').disabled=true;$('publish-button').textContent='Отправляем…';$('publish-feedback').hidden=true;
  try{const result=await api('/api/publish',{method:'POST',body:JSON.stringify(request)});const message=result.message;
    if(message.status==='published'){feedback(`Сообщение №${message.post_number} опубликовано от имени ${message.nickname}.`);$('post-text').value='';request=null;}
    else if(message.status==='failed'){feedback('Telegram отклонил публикацию. Проверьте права бота и настройки канала перед новой попыткой.',true);request=null;}
    else feedback('Результат отправки не подтверждён. Проверьте канал и журнал. Повторное нажатие не отправит это сообщение заново.',true);
    page=1;await load();
  }catch(error){feedback(error.message+' Проверьте журнал; повторное нажатие безопасно.',true);}
  finally{publishing=false;$('publish-button').disabled=false;$('post-text').disabled=false;$('role').disabled=false;$('publish-button').textContent='Опубликовать в канал';preview();}
});
async function detail(id){try{const message=await api('/api/messages/'+id);$('detail-title').textContent=message.post_number?'Публикация №'+message.post_number:'Сообщение #'+message.id;const root=$('detail-content');root.replaceChildren();const grid=node('dl','detail-grid');const values=[['Отправитель',message.sender_name||'Без имени'],['Telegram ID',message.sender_id??'Публикация из панели'],['Username',message.sender_username?'@'+message.sender_username:'Не указан'],['Ник при отправке',message.nickname||'Без ника'],['Время отправки',new Date(message.sent_at*1000).toLocaleString('ru-RU')],['Тип',mediaNames[message.media_type]||message.media_type]];for(const[label,value]of values){const cell=node('div');cell.append(node('dt','',label),node('dd','',String(value)));grid.append(cell);}root.append(grid,badge(message.status));if(message.reason)root.append(node('p','detail-reason',reasonLabels[message.reason]||message.reason));root.append(node('p','detail-body',message.body||'Без текста'));
  if(message.has_media){const url='/api/messages/'+id+'/media';let media;if(message.media_type==='photo'){media=node('img','detail-media');media.alt='Фото из сообщения';}else if(['video','video_note','animation'].includes(message.media_type)){media=node('video','detail-media');media.controls=true;media.preload='metadata';}else if(['voice','audio'].includes(message.media_type)){media=node('audio','detail-media');media.controls=true;media.preload='metadata';}if(media){media.src=url;media.addEventListener('error',()=>{media.replaceWith(node('p','detail-reason','Вложение недоступно для предпросмотра. Попробуйте открыть файл или публикацию в Telegram.'));});root.append(media);}const link=node('a','detail-link','Открыть вложение');link.href=url;link.target='_blank';link.rel='noreferrer';root.append(link);}
  if(message.details&&Object.keys(message.details).length)root.append(node('pre','detail-data',JSON.stringify(message.details,null,2)));
  if(message.channel_url){const link=node('a','detail-link','Открыть в Telegram');link.href=message.channel_url;link.target='_blank';link.rel='noreferrer';root.append(link);}if(!$('detail-dialog').open)$('detail-dialog').showModal();
}catch(error){showError(error.message);}}
$('close-detail').addEventListener('click',()=>{$('detail-dialog').close();$('detail-content').replaceChildren();});$('detail-dialog').addEventListener('click',event=>{if(event.target===$('detail-dialog')){const bounds=event.target.getBoundingClientRect();if(event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<bounds.top||event.clientY>bounds.bottom)event.target.close();}});
setInterval(()=>{if(csrf&&!document.hidden)load();},10000);document.addEventListener('visibilitychange',()=>{if(!document.hidden&&csrf)load();});preview();api('/api/session').then(enter).catch(()=>{});

/* Casa Volterra TV Extension — content.js v5.2 */
(function () {
  'use strict';

  var PROXY        = 'http://127.0.0.1:8765';
  var POLL_MS      = 400;   // polling scroll commands
  var SCROLL_Y     = 300;
  var SCROLL_X     = 200;

  // Non girare sul proxy stesso (porta 8765) — OK su Home Assistant (8123) e altri
  if (location.port === '8765') return;

  /* ─── Util ─────────────────────────────────────────────── */
  function xhrGet(url, cb) {
    var x = new XMLHttpRequest();
    x.open('GET', url, true);
    x.timeout = 1500;
    x.onload = function () { if (cb) cb(x.responseText); };
    x.onerror = x.ontimeout = function () { if (cb) cb(null); };
    try { x.send(); } catch(e) { if (cb) cb(null); }
  }

  /* ─── FAB: Home + Back ──────────────────────────────────── */
  var fab = document.createElement('div');
  fab.id = 'cvtv-fab-wrap';

  var btnHome = document.createElement('button');
  btnHome.title = 'TV Screen';
  btnHome.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>';
  btnHome.addEventListener('click', function () {
    btnHome.classList.add('going');
    xhrGet(PROXY + '/launch/tv');
  });

  var btnBack = document.createElement('button');
  btnBack.title = 'Back';
  btnBack.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>';
  btnBack.addEventListener('click', function () {
    if (window.history.length > 1) window.history.back();
    else xhrGet(PROXY + '/launch/tv');
  });

  fab.appendChild(btnHome);
  fab.appendChild(btnBack);
  document.body.appendChild(fab);

  /* ─── Posizione FAB configurabile per sito ──────────────── */
  xhrGet(PROXY + '/remote/fab-position?host=' + encodeURIComponent(location.hostname), function(txt) {
    if (!txt) return;
    try {
      var pos = JSON.parse(txt);
      var s = 'position:fixed;z-index:2147483647;display:flex;flex-direction:column;gap:10px;pointer-events:none;';
      if (pos.top    != null) s += 'top:'    + pos.top    + 'px;';
      if (pos.right  != null) s += 'right:'  + pos.right  + 'px;';
      if (pos.bottom != null) s += 'bottom:' + pos.bottom + 'px;';
      if (pos.left   != null) s += 'left:'   + pos.left   + 'px;';
      fab.style.cssText = s;
    } catch(e) {}
  });

  /* ─── SCROLL POLLING ────────────────────────────────────── */
  // Il proxy riceve i tasti freccia via evdev e li salva.
  // L'extension fa polling e scrolla la pagina.
  // Polling solo quando la tastiera NON è aperta.
  var _kbOpen = false;
  var _pollTimer = null;

  function startPoll() {
    if (_pollTimer) return;
    _pollTimer = setTimeout(doPoll, POLL_MS);
  }

  function doPoll() {
    _pollTimer = null;
    if (_kbOpen) { _pollTimer = setTimeout(doPoll, POLL_MS); return; }

    xhrGet(PROXY + '/remote/browser-commands', function (txt) {
      if (txt) {
        try {
          var d = JSON.parse(txt);
          var isStreaming = (d && (d.app === 'netflix' || d.app === 'prime' || d.app === 'disney' || d.app === 'kodi'));
          if (d && d.scroll && !isStreaming) doScroll(d.scroll);
          if (d && d.back && !isStreaming) {
            if (window.history.length > 1) window.history.back();
            else xhrGet(PROXY + '/launch/browser/home');
          }
          if (d && d.playpause) {
            ['keydown','keyup'].forEach(function(t) {
              try {
                document.dispatchEvent(new KeyboardEvent(t, {
                  key: ' ', code: 'Space', keyCode: 32, which: 32,
                  bubbles: true, cancelable: true
                }));
              } catch(e2) {}
            });
            var pbtn = document.querySelector(
              '[data-uia="control-play-pause"], .play-pause-button, [aria-label*="Pause"], [aria-label*="Play"]'
            );
            if (pbtn) pbtn.click();
          }
        } catch(e) {}
      }
      _pollTimer = setTimeout(doPoll, POLL_MS);
    });
  }

  function doScroll(dir) {
    var el = bestScrollTarget();
    if (dir === 'up')    scrollEl(el,  0,  -SCROLL_Y);
    if (dir === 'down')  scrollEl(el,  0,   SCROLL_Y);
    if (dir === 'left')  scrollEl(el, -SCROLL_X, 0);
    if (dir === 'right') scrollEl(el,  SCROLL_X, 0);
  }

  function bestScrollTarget() {
    var cands = [
      document.querySelector('main'),
      document.querySelector('[role="main"]'),
      document.querySelector('article'),
      document.querySelector('#content'),
      document.querySelector('.content'),
    ];
    for (var i = 0; i < cands.length; i++) {
      if (cands[i] && canScroll(cands[i])) return cands[i];
    }
    return window;
  }

  function canScroll(el) {
    try {
      var s = window.getComputedStyle(el);
      return (/scroll|auto/).test((s.overflow||'')+(s.overflowY||'')) &&
             el.scrollHeight > el.clientHeight + 4;
    } catch(e) { return false; }
  }

  function scrollEl(el, dx, dy) {
    try {
      if (!el || el === window) { window.scrollBy({left:dx,top:dy,behavior:'smooth'}); return; }
      el.scrollBy({left:dx,top:dy,behavior:'smooth'});
    } catch(e) { try { window.scrollBy(dx, dy); } catch(e2) {} }
  }

  // Avvia polling dopo 1.5s (aspetta che la pagina sia pronta)
  setTimeout(startPoll, 1500);

  /* ─── VIRTUAL KEYBOARD ──────────────────────────────────── */
  var KBR = {
    lo: [
      ['q','w','e','r','t','y','u','i','o','p'],
      ['a','s','d','f','g','h','j','k','l'],
      ['⇧','z','x','c','v','b','n','m','⌫'],
      ['?123','·','⏎','✕']
    ],
    up: [
      ['Q','W','E','R','T','Y','U','I','O','P'],
      ['A','S','D','F','G','H','J','K','L'],
      ['⇧','Z','X','C','V','B','N','M','⌫'],
      ['?123','·','⏎','✕']
    ],
    num: [
      ['1','2','3','4','5','6','7','8','9','0'],
      ['-','/',':',';','(',')','€','&','@','"'],
      ['.',',','?','!','\'','+','=','#','%','⌫'],
      ['ABC','·','⏎','✕']
    ]
  };

  var kbEl = document.createElement('div');
  kbEl.id = 'cvtv-kb';
  kbEl.innerHTML = '<div id="cvtv-kb-handle"></div><div id="cvtv-kb-rows"></div>';
  document.body.appendChild(kbEl);

  var kbRows    = kbEl.querySelector('#cvtv-kb-rows');
  var kbHandle  = kbEl.querySelector('#cvtv-kb-handle');
  var _shifted  = false;
  var _numMode  = false;
  var _activeEl = null;
  var _blurTimer = null;

  kbHandle.addEventListener('mousedown', function(e){e.preventDefault();kbHide();});

  function kbCurrent() {
    if (_numMode) return KBR.num;
    return _shifted ? KBR.up : KBR.lo;
  }

  function kbRender() {
    kbRows.innerHTML = '';
    kbCurrent().forEach(function(row) {
      var div = document.createElement('div');
      div.className = 'cvtv-kb-row';
      row.forEach(function(k) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'cvtv-k';
        var lbl = k;
        switch(k) {
          case '·':   btn.classList.add('ksp'); lbl='SPACE'; break;
          case '⏎':   btn.classList.add('kw','kact'); lbl='⏎ Go'; break;
          case '✕':   btn.classList.add('kw','kact'); lbl='✕ Close'; break;
          case '⇧':   btn.classList.add('kw'); lbl='⇧ Shift';
                      if(_shifted) btn.classList.add('kshift-on'); break;
          case '⌫':   btn.classList.add('kw'); break;
          case '?123':case 'ABC': btn.classList.add('kw'); break;
        }
        btn.textContent = lbl;
        btn.addEventListener('mousedown', function(e) {
          e.preventDefault(); e.stopPropagation();
          kbKey(k);
        });
        div.appendChild(btn);
      });
      kbRows.appendChild(div);
    });
  }

  function kbKey(k) {
    if (!_activeEl) return;
    if (k === '✕') { kbHide(); return; }
    if (k === '⏎') {
      var form = null;
      try { form = _activeEl.closest('form'); } catch(e) {}
      if (form) {
        var sb = form.querySelector('[type="submit"]');
        if (sb) sb.click();
        else { try{form.requestSubmit();}catch(e){try{form.submit();}catch(e2){}} }
      } else {
        ['keydown','keypress','keyup'].forEach(function(t){
          try{_activeEl.dispatchEvent(new KeyboardEvent(t,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));}catch(e){}
        });
      }
      kbHide(); return;
    }
    if (k==='⇧')  { _shifted=!_shifted;_numMode=false;kbRender();return; }
    if (k==='?123'){ _numMode=true;_shifted=false;kbRender();return; }
    if (k==='ABC') { _numMode=false;_shifted=false;kbRender();return; }
    if (k==='·')   k=' ';
    if (k==='⌫')   { kbBS(); return; }
    kbIns(k);
    if(_shifted&&!_numMode){_shifted=false;kbRender();}
  }

  function kbIns(ch) {
    if (!_activeEl) return;
    try {
      _activeEl.focus();
      if (document.execCommand('insertText', false, ch)) return;
    } catch(e) {}
    var el=_activeEl, s=el.selectionStart||0, e2=el.selectionEnd||s;
    el.value=(el.value||'').slice(0,s)+ch+(el.value||'').slice(e2);
    el.selectionStart=el.selectionEnd=s+1;
    kbFire(el);
  }

  function kbBS() {
    if (!_activeEl) return;
    try {
      _activeEl.focus();
      if (document.execCommand('delete',false)) return;
    } catch(e) {}
    var el=_activeEl, s=el.selectionStart, e2=el.selectionEnd;
    if(s!==e2){el.value=(el.value||'').slice(0,s)+(el.value||'').slice(e2);el.selectionStart=el.selectionEnd=s;}
    else if(s>0){el.value=(el.value||'').slice(0,s-1)+(el.value||'').slice(s);el.selectionStart=el.selectionEnd=s-1;}
    kbFire(el);
  }

  function kbFire(el) {
    ['input','change'].forEach(function(t){
      try{el.dispatchEvent(new Event(t,{bubbles:true,cancelable:true}));}catch(e){}
    });
  }

  function kbShow(el) {
    clearTimeout(_blurTimer);
    _activeEl = el;
    _shifted  = false;
    var t = (el.type||'').toLowerCase();
    _numMode  = (t==='number'||t==='tel');
    kbRender();
    kbEl.classList.add('kb-visible');
    _kbOpen = true;
    setTimeout(function(){
      try{el.scrollIntoView({behavior:'smooth',block:'center'});}catch(e){}
    }, 230);
  }

  function kbHide() {
    clearTimeout(_blurTimer);
    kbEl.classList.remove('kb-visible');
    _kbOpen   = false;
    _activeEl = null;
  }

  /* ─── Focus detection ───────────────────────────────────── */
  var SKIP = ['submit','button','reset','checkbox','radio','range','color','file','image','hidden'];

  function isText(el) {
    if (!el) return false;
    var tag = (el.tagName||'').toUpperCase();
    if (tag !== 'INPUT' && tag !== 'TEXTAREA') return false;
    return SKIP.indexOf((el.type||'text').toLowerCase()) === -1;
  }

  // Usa capture=true per essere sicuri di prendere il focus PRIMA del sito
  document.addEventListener('focus', function(e) {
    if (isText(e.target)) kbShow(e.target);
  }, true);

  document.addEventListener('blur', function(e) {
    if (!isText(e.target)) return;
    clearTimeout(_blurTimer);
    _blurTimer = setTimeout(function() {
      var f = document.activeElement;
      if (kbEl.contains(f)) return;
      if (isText(f)) return; // focus su altro input
      kbHide();
    }, 200);
  }, true);

  // Click fuori
  document.addEventListener('click', function(e) {
    if (!_kbOpen) return;
    if (kbEl.contains(e.target) || fab.contains(e.target)) return;
    if (isText(e.target)) return;
    kbHide();
  }, true);

  // Escape fisico
  document.addEventListener('keydown', function(e) {
    if (e.key==='Escape' && _kbOpen) { kbHide(); e.stopPropagation(); }
  }, true);

  // MutationObserver per SPA (React, Vue, Angular)
  try {
    new MutationObserver(function() {
      if (_kbOpen) return;
      var f = document.activeElement;
      if (isText(f) && f !== _activeEl) kbShow(f);
    }).observe(document.body, {childList:true, subtree:true});
  } catch(e) {}

  // Fallback polling leggero ogni 2s per SPA che non emettono eventi focus
  setInterval(function() {
    if (_kbOpen) return;
    var f = document.activeElement;
    if (isText(f) && f !== _activeEl) kbShow(f);
  }, 2000);

  kbRender();
})();

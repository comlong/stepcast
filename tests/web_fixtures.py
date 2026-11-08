"""Synthetic HTML decks for the tests (made up here; never a real deck).

custom_deck(): the kind of deck an AI writes — <section class="slide"> on a 1280x720 stage scaled to the window, notes in data-notes
  with stage directions, #slide-N in the address, arrows / dots / a top bar around the stage, buttons that start animations.
reveal_like(): a deck exposing a Reveal-style API (getSlides / slide / getSlideNotes / nextFragment) with fragments.
keys_only(): slides that change only with the → key (nothing in the address).
shower_like(): a list of all slides with a full mode, through a Shower-style API, with inner steps.
framework_bits(): class-named hidden notes, capitals by CSS, slide changes inside view transitions.
cards_deck(): cards, a list and a button a narration can name (highlights, what a button brings up).
sliding_track(): slides on a track that slides along (a transition), page dots with "transition: all".
scroll_deck(): one long page, a slide under the other.
pages_zip(dir): one HTML file per slide, with a picture in a sub folder, zipped.
"""
import zipfile
from pathlib import Path

STYLE = """
*{box-sizing:border-box;margin:0;padding:0}html,body{height:100%}
body{background:#dde3ee;font-family:"Segoe UI","Microsoft YaHei",sans-serif;overflow:hidden;display:flex;flex-direction:column}
.topbar{flex:none;height:34px;background:#123;color:#fff;font-size:13px;padding:8px 14px}
.viewport{flex:1;position:relative;overflow:hidden}
.stage{width:1280px;height:720px;position:absolute;left:50%;top:50%;background:#f7f8fb;border-radius:14px;box-shadow:0 10px 40px #0003;
 overflow:hidden;transform-origin:center}
.slide{position:absolute;inset:0;padding:60px 80px;display:none;flex-direction:column}
.slide.active{display:flex}
h1{font-size:70px;color:#14213d}h2{font-size:46px;color:#14213d;margin-bottom:24px}
.rise{animation:rise .6s ease-out both;animation-delay:calc(var(--i,0)*120ms)}
@keyframes rise{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
.tabs{display:flex;gap:12px;margin-bottom:24px}
.tabs button{font-size:22px;padding:10px 22px;border:2px solid #ccd;background:#fff;border-radius:10px}
.tabs button.sel{border-color:#e4572e;color:#e4572e}
.panel{font-size:30px;color:#333;height:60px}
.btn{font-size:24px;padding:12px 26px;background:#14213d;color:#fff;border:0;border-radius:10px;width:max-content}
.bar{height:36px;background:#e4572e;width:0;margin-top:30px;transition:width 1s linear}
.bar.full{width:900px}
.hot{position:absolute;width:44px;height:44px;border-radius:50%;background:#e4572e;color:#fff;border:3px solid #fff;font-size:20px}
.hot.sel{background:#2f6bff}
#out{font-size:28px;margin-top:20px;height:40px;color:#14213d}
.arrow{position:absolute;top:50%;width:44px;height:44px;border-radius:50%;border:1px solid #ccd;background:#fff;z-index:5}
.arrow.l{left:10px}.arrow.r{right:10px}
.nav{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);display:flex;gap:6px;z-index:5}
.nav button{width:12px;height:12px;border-radius:50%;border:0;background:#99a}
.nav button.sel{background:#e4572e;width:28px;border-radius:9px}
"""


def custom_deck() -> str:
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>示例演示稿</title><style>""" + STYLE + """</style></head>
<body>
<div class="topbar">示例 · 按 → 翻页 <button id="timer">开始计时</button></div>
<div class="viewport" id="vp">
  <button class="arrow l" id="prev" aria-label="上一页">‹</button>
  <div class="stage" id="stage">
    <section class="slide" data-notes="大家好，今天介绍一个示例产品。">
      <h1 class="rise" style="--i:0">示例产品</h1>
      <p class="rise" style="--i:1;font-size:30px;margin-top:20px">一句话说明它做什么</p>
    </section>
    <section class="slide" data-notes="它有三种用法。第一种是快速模式，适合日常小事；第二种是精确模式，结果更严谨；第三种是批量模式，一次处理很多文件。">
      <h2 class="rise">三种用法</h2>
      <div class="tabs rise" style="--i:1" id="tabs">
        <button type="button" data-t="0" class="sel">快速模式</button><button type="button" data-t="1">精确模式</button><button type="button" data-t="2">批量模式</button>
      </div>
      <div class="panel" id="panel">日常小事</div>
    </section>
    <section class="slide" data-notes="看一个例子。我们让它处理一份表格。（点按钮）它会先读文件，再一步步算出结果，最后把结果写回表格。">
      <h2 class="rise">动手看看</h2>
      <button class="btn rise" style="--i:1" id="run" type="button">▶ 运行演示</button>
      <div class="bar" id="bar"></div>
      <div id="out"></div>
    </section>
    <section class="slide" data-notes="界面上有三个地方要认识。第一，收件箱，每封邮件分开显示；第二，日历，按周显示安排；第三，通讯录，按部门排列。（依次点 1 到 3 号热点）">
      <h2 class="rise">认识界面</h2>
      <div style="position:relative;width:700px;height:380px;background:#fff;border:1px solid #ccd;border-radius:12px" class="rise">
        <button class="hot" type="button" data-c="0" aria-label="收件箱" style="left:40px;top:40px">1</button>
        <button class="hot" type="button" data-c="1" aria-label="日历" style="left:300px;top:200px">2</button>
        <button class="hot" type="button" data-c="2" aria-label="通讯录" style="left:560px;top:300px">3</button>
      </div>
      <div id="concept" style="font-size:30px;margin-top:16px">收件箱</div>
    </section>
  </div>
  <button class="arrow r" id="next" aria-label="下一页">›</button>
  <div class="nav" id="dots"></div>
</div>
<script>
(function(){
  const slides=[...document.querySelectorAll('.slide')], stage=document.getElementById('stage'), vp=document.getElementById('vp');
  let cur=0;
  function fit(){const s=Math.min((vp.clientWidth-110)/1280,(vp.clientHeight-40)/720);stage.style.transform='translate(-50%,-50%) scale('+s+')';}
  addEventListener('resize',fit);
  const dots=document.getElementById('dots');
  slides.forEach((_,i)=>{const b=document.createElement('button');b.onclick=()=>go(i);dots.appendChild(b);});
  function go(i){i=Math.max(0,Math.min(slides.length-1,i));slides[cur].classList.remove('active');cur=i;
    slides[cur].querySelectorAll('.rise').forEach(el=>{el.style.animation='none';void el.offsetWidth;el.style.animation='';});
    slides[cur].classList.add('active');[...dots.children].forEach((d,k)=>d.classList.toggle('sel',k===cur));
    history.replaceState(null,'','#slide-'+(cur+1));}
  document.getElementById('prev').onclick=()=>go(cur-1);document.getElementById('next').onclick=()=>go(cur+1);
  addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key===' ')go(cur+1);else if(e.key==='ArrowLeft')go(cur-1);});
  const P=['日常小事','结果更严谨','一次处理很多文件'];
  document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('sel',x===b));document.getElementById('panel').textContent=P[+b.dataset.t];});
  document.getElementById('run').onclick=function(){const bar=document.getElementById('bar'),out=document.getElementById('out');
    bar.classList.remove('full');void bar.offsetWidth;bar.classList.add('full');out.textContent='读取文件…';
    setTimeout(()=>out.textContent='计算中…',600);setTimeout(()=>{out.textContent='完成 '+new Date().getFullYear();document.body.dataset.done='1';},1500);};
  const C=['收件箱','日历','通讯录'];
  document.querySelectorAll('.hot').forEach(h=>h.onclick=()=>{document.querySelectorAll('.hot').forEach(x=>x.classList.toggle('sel',x===h));document.getElementById('concept').textContent=C[+h.dataset.c];});
  const m=location.hash.match(/slide-(\\d+)/);go(m?(+m[1]-1):0);fit();
})();
</script></body></html>"""


def reveal_like() -> str:
    """A tiny deck with a Reveal-style API (StepCast reads it through the API, as it does real reveal.js decks)."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#222;color:#eee;font:28px sans-serif}
.reveal{position:absolute;inset:0}.slides section{position:absolute;inset:0;padding:80px;display:none}
.slides section.present{display:block}.fragment{opacity:0;transition:opacity .4s}.fragment.visible{opacity:1}
.controls{position:absolute;right:10px;bottom:10px;width:80px;height:40px;background:#e33}
</style></head><body><div class="reveal"><div class="slides">
<section><h2>Opening</h2><aside class="notes">Welcome to the overview.</aside></section>
<section><h2>Three points</h2><p class="fragment">Alpha arrives first</p><p class="fragment">Beta follows next</p>
<aside class="notes">Here are the points. Alpha arrives first in the list. Then beta follows next.</aside></section>
<section><h2>Closing</h2><aside class="notes">Thanks for watching.</aside></section>
</div><div class="controls"></div></div>
<script>
window.Reveal=(function(){const S=[...document.querySelectorAll('.slides section')];let h=0,f=-1;
 function show(){S.forEach((s,i)=>s.classList.toggle('present',i===h));S[h].querySelectorAll('.fragment').forEach((x,k)=>x.classList.toggle('visible',k<=f));}
 return{getTotalSlides:()=>S.length,getSlides:()=>S,getIndices:(el)=>({h:S.indexOf(el),v:0}),getCurrentSlide:()=>S[h],
  slide:(i)=>{h=i;f=-1;show();},getSlideNotes:(el)=>(el||S[h]).querySelector('aside.notes').innerHTML,
  nextFragment:()=>{f++;show();},configure:()=>{},isReady:()=>true};})();
Reveal.slide(0);
</script></body></html>"""


def keys_only() -> str:
    """Slides that change only with →; the second one shows its two parts one at a time first (builds)."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;font:30px sans-serif}.page{position:absolute;inset:0;padding:60px;display:none;background:#fff}
.page.on{display:block}.part{visibility:hidden}.part.shown{visibility:visible}
</style></head><body>
<div class="page on"><h1>One</h1><div class="notes" style="display:none">First page notes.</div></div>
<div class="page"><h1>Two</h1><p class="part">Part A</p><p class="part">Part B</p></div>
<div class="page"><h1>Three</h1></div>
<script>
const P=[...document.querySelectorAll('.page')];let k=0,b=0;
addEventListener('keydown',e=>{if(e.key!=='ArrowRight')return;const parts=P[k].querySelectorAll('.part');
 if(b<parts.length){parts[b].classList.add('shown');b++;return;}
 if(k<P.length-1){P[k].classList.remove('on');k++;b=0;P[k].classList.add('on');}});
</script></body></html>"""


def shower_like() -> str:
    """Slides listed side by side until a full mode shows one at a time, through a Shower-style API (window.shower: slides, goTo,
    next, enterFullMode); inner steps (.next) on the second slide; a progress bar 0 px wide that draws itself with ::after."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;font:32px sans-serif;background:#ccc}
body.list{display:flex;flex-wrap:wrap;gap:20px;padding:20px;box-sizing:border-box}
body.list .slide{width:400px;height:225px;background:#fff;overflow:hidden;font-size:12px}
body.full .slide{display:none;position:fixed;inset:0;background:#fff;padding:60px;box-sizing:border-box}
body.full .slide.active{display:block}
.slide .next{visibility:hidden}.slide .next.active,.slide .next.visited{visibility:visible}
.progress{display:none}
body.full .progress{display:block;position:fixed;left:0;bottom:0;height:14px;width:0}
body.full .progress::after{content:'';position:absolute;left:0;top:0;height:14px;width:var(--p,0px);background:#e33}
</style></head><body class="shower list">
<section class="slide" id="cover"><h2>Cover page</h2></section>
<section class="slide" id="steps"><h2>Steps page</h2><p class="next">Step one</p><p class="next">Step two</p></section>
<section class="slide" id="end"><h2>End page</h2></section>
<div class="progress"></div>
<script>
(function(){const els=[...document.querySelectorAll('.slide')];let cur=0,mode='list';
 const o={stepSelector:'.next',activeSlideClass:'active',visitedSlideClass:'visited'};
 const slides=els.map((element,i)=>({element,get isActive(){return i===cur;}}));
 function paint(){els.forEach((e,i)=>e.classList.toggle('active',i===cur));document.body.classList.toggle('full',mode==='full');
  document.body.classList.toggle('list',mode==='list');
  document.querySelector('.progress').style.setProperty('--p',Math.round((cur+1)/els.length*innerWidth)+'px');}
 const S={slides,options:o,container:document.body,get isFullMode(){return mode==='full';},get activeSlideIndex(){return cur;},
  enterFullMode(){mode='full';paint();},exitFullMode(){mode='list';paint();},
  goTo(i){if(!els[i])return;cur=i;els[i].querySelectorAll('.next').forEach(n=>n.classList.remove('active','visited'));paint();},
  next(){const st=[...els[cur].querySelectorAll('.next')];
   const done=st.filter(n=>n.classList.contains('active')||n.classList.contains('visited')).length;
   if(done<st.length){st.forEach((n,k)=>{n.classList.toggle('visited',k<done);n.classList.toggle('active',k===done);});return;}
   S.goTo(cur+1);}};
 Object.defineProperty(window,'shower',{value:S,configurable:true});paint();})();
</script></body></html>"""


def framework_bits() -> str:
    """Things deck tools do: notes in a hidden box named by its class (deck-slide-notes), headings shown in capitals by CSS, slides
    changed inside a view transition, the slides' container called "impress" without impress.js on the page."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;font:30px sans-serif;background:#fff}
.slide{position:absolute;inset:0;padding:60px;display:none}.slide.on{display:block}
h2{text-transform:uppercase;color:#234}
</style></head><body><div id="impress">
<section class="slide step on"><h2>Mixed Case Title</h2>
 <div class="deck-slide-notes" hidden>Notes kept in a hidden box of their own.</div></section>
<section class="slide step"><h2>Second Page</h2><div class="deck-slide-notes" hidden>Second page notes.</div></section>
<section class="slide step"><h2>Third Page</h2></section>
</div><script>
const S=[...document.querySelectorAll('.slide')];let k=0;
function go(n){S[k].classList.remove('on');k=n;S[k].classList.add('on');}
addEventListener('keydown',e=>{if(e.key!=='ArrowRight'||k>=S.length-1)return;const n=k+1;
 if(document.startViewTransition)document.startViewTransition(()=>go(n));else go(n);});
</script></body></html>"""


def sliding_track() -> str:
    """Slides side by side on a track that slides along with → (a CSS transition), page dots with "transition: all" over them."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;overflow:hidden;font:40px sans-serif}
.track{display:flex;height:100%;transition:transform .8s ease}
.slide{flex:0 0 100vw;height:100%;padding:60px;box-sizing:border-box;color:#fff}
.s1{background:#cc3333}.s2{background:#33aa33}.s3{background:#3333cc}
.dots{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);display:flex;gap:10px;transition:all .3s}
.dots i{display:block;width:18px;height:18px;border-radius:50%;background:#ffdd00}
</style></head><body>
<div class="track"><section class="slide s1"><h1>Red</h1></section><section class="slide s2"><h1>Green</h1></section>
<section class="slide s3"><h1>Blue</h1></section></div>
<div class="dots"><i></i><i></i><i></i></div>
<script>let k=0;const T=document.querySelector('.track');
addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&k<2){k++;T.style.transform='translateX('+(-k*100)+'vw)';}});</script>
</body></html>"""


SAY = "This narration is long enough to keep the slide on the screen for several seconds while the moving part of it plays."
BASE = """html,body{margin:0;height:100%;font:30px sans-serif;background:#fff}
.slide{position:absolute;inset:0;display:none}.slide.on{display:block}"""
TURN = """<script>const S=[...document.querySelectorAll('.slide')];let k=0;addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&k<S.length-1){
S[k].classList.remove('on');k++;S[k].classList.add('on');}});</script>"""


def smil_deck() -> str:
    """An SVG drawing that moves by itself (<animate>, no script): a red dot crossing the screen in 4 seconds."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}
svg{{position:absolute;left:0;top:0;width:100%;height:100%}}</style></head><body>
<section class="slide on" data-notes="{SAY}"><svg viewBox="0 0 1920 1080"><circle cx="100" cy="540" r="90" fill="#e03030">
<animate attributeName="cx" from="100" to="1800" dur="4s" fill="freeze"/></circle></svg></section>
<section class="slide" data-notes="Second."><h1>Two</h1></section>{TURN}</body></html>"""


def gif_zip(folder: Path) -> Path:
    """A GIF of four colours, half a second each."""
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    frames = [Image.new("RGB", (320, 180), c) for c in ((220, 40, 40), (40, 180, 70), (50, 80, 220), (240, 200, 30))]
    frames[0].save(folder / "demo.gif", save_all=True, append_images=frames[1:], duration=500, loop=0)
    z = folder / "gif.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(folder / "demo.gif", "demo.gif")
        zf.writestr("index.html", f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}
img{{width:100%;height:100%;display:block}}</style></head><body>
<section class="slide on" data-notes="{SAY}"><img src="demo.gif" alt="demo"></section>
<section class="slide" data-notes="Second."><h1>Two</h1></section>{TURN}</body></html>""")
    return z


def dialog_deck() -> str:
    """Dialogs: alert() while loading, "leave this page?", and confirm / prompt behind a button."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}#o{{width:50%;height:80%;background:#ddd}}</style></head><body>
<section class="slide on" data-notes="{SAY} (click the Go button)"><button id="b" style="font-size:40px">Go</button><div id="o"></div></section>
<section class="slide" data-notes="Second."><h1>Two</h1></section>{TURN}
<script>alert('welcome');addEventListener('beforeunload',e=>{{e.preventDefault();e.returnValue='';}});
document.getElementById('b').onclick=()=>{{if(confirm('sure?')&&prompt('who?','Zed')==='Zed')document.getElementById('o').style.background='#20b040';}};
</script></body></html>"""


def frame_zip(folder: Path) -> Path:
    """A slide with a page of the deck in an <iframe>: a bar growing over 2 s (CSS) and a colour change after 1 s (timer)."""
    folder.mkdir(parents=True, exist_ok=True)
    z = folder / "frame.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("index.html", f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}
iframe{{width:100%;height:100%;border:0;display:block}}</style></head><body>
<section class="slide on" data-notes="{SAY}"><iframe src="demo.html"></iframe></section>
<section class="slide" data-notes="Second."><h1>Two</h1></section>{TURN}</body></html>""")
        zf.writestr("demo.html", """<!doctype html><style>html,body{margin:0;height:100%}
#b{height:50%;width:0;background:#22aa77;animation:g 2s linear forwards}@keyframes g{to{width:100%}}#t{width:100%;height:50%;background:#eee}</style>
<div id="b"></div><div id="t"></div><script>setTimeout(()=>{document.getElementById('t').style.background='#e03030'},1000)</script>""")
    return z


def viewer_zip(folder: Path) -> Path:
    """A viewer page around one HTML file per slide (an <iframe> whose src changes with →); notes and a button inside the files."""
    folder.mkdir(parents=True, exist_ok=True)
    z = folder / "viewer.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("index.html", """<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%}
iframe{position:absolute;inset:0;width:100%;height:100%;border:0}</style></head><body><iframe id="f" src="s1.html"></iframe>
<script>let k=1;const f=document.getElementById('f');addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&k<3){k++;f.src='s'+k+'.html';}});</script></body></html>""")
        for n, color in ((1, "#fde"), (2, "#def"), (3, "#efd")):
            zf.writestr(f"s{n}.html", f"""<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;height:100%;background:{color};font:30px sans-serif}}
#o{{width:50%;height:70%;background:#ddd}}.notes{{display:none}}</style><h1>Page {n}</h1><button id="r" onclick="document.getElementById('o').style.background='#20b040'">Run</button>
<div id="o"></div><div class="notes">{SAY} (click the Run button) Notes of page {n}.</div>""")
    return z


def script_notes_list() -> str:
    """Notes only in the deck's script, inside a function: an array with one text per slide."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}</style></head><body>
<section class="slide on"><h1>First</h1></section><section class="slide"><h1>Second</h1></section>{TURN}
<script>(function(){{ // the presenter view would show these
 const speakerNotes = ["Notes of the first slide.", 'Notes of the second slide, with a "quote" in it.'];
 window.showNotes = (k) => speakerNotes[k];
}})();</script></body></html>"""


def script_notes_objects() -> str:
    """A deck drawn from a list of slide objects (one element whose content changes); the notes are a field of each."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}.page{{padding:60px}}</style></head><body><div class="page" id="p"></div><script>
const pages=[{{title:'Alpha page',body:'aaa',notes:'Notes of the alpha page.'}},{{title:'Beta page',body:'bbb',notes:'Notes of the beta page.'}},
 {{title:'Gamma page',body:'ccc',notes:'Notes of the gamma page.'}}];
let k=0;const p=document.getElementById('p');function r(){{p.innerHTML='<h1>'+pages[k].title+'</h1><p>'+pages[k].body+'</p>';}}
addEventListener('keydown',e=>{{if(e.key==='ArrowRight'&&k<pages.length-1){{k++;r();}}}});r();
</script></body></html>"""


def canvas_deck(turn: str = "keydown") -> str:
    """Slides drawn on a canvas: no text on the page. Turned with → (or, with turn="click", by a click anywhere)."""
    cond = "e.key==='ArrowRight'&&" if turn == "keydown" else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>html,body{{margin:0;height:100%;overflow:hidden}}canvas{{position:absolute;inset:0}}</style></head>
<body><canvas id="c" width="1920" height="1080"></canvas><script>const C=['#cc3333','#33aa33','#3333cc'];let k=0;const g=document.getElementById('c').getContext('2d');
function d(){{g.fillStyle=C[k];g.fillRect(0,0,1920,1080);}}addEventListener('{turn}',e=>{{if({cond}k<2){{k++;d();}}}});d();</script></body></html>"""


def moving_canvas_deck() -> str:
    """A canvas that keeps moving by itself (a bouncing block) on top of slides turned with →: the movement is not a new page."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%;overflow:hidden}canvas{position:absolute;inset:0}</style></head>
<body><canvas id="c" width="1920" height="1080"></canvas><script>const C=['#cc3333','#33aa33'];let k=0,x=0;const g=document.getElementById('c').getContext('2d');
function d(){g.fillStyle=C[k];g.fillRect(0,0,1920,1080);g.fillStyle='#fff';g.fillRect(100+(x%400),100,60,60);x+=7;requestAnimationFrame(d);}
addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&k<1)k++;});d();</script></body></html>"""


def cover_deck() -> str:
    """A start screen over the slides: nothing can be turned until its button is pressed."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}
#cover{{position:fixed;inset:0;background:#123456;display:flex;align-items:center;justify-content:center;z-index:9}}</style></head><body>
<div id="cover"><button id="go" style="font-size:40px">开始演示</button></div>
<section class="slide on" data-notes="One."><h1>Slide one</h1></section><section class="slide" data-notes="Two."><h1>Slide two</h1></section>
<script>let started=false;document.getElementById('go').onclick=()=>{{started=true;document.getElementById('cover').remove();}};
const S=[...document.querySelectorAll('.slide')];let k=0;addEventListener('keydown',e=>{{if(started&&e.key==='ArrowRight'&&k<1){{S[k].classList.remove('on');k++;S[k].classList.add('on');}}}});</script>
</body></html>"""  # i18n: ignore


def spa_zip(folder: Path) -> Path:
    """A built web app with a router on real paths (/1, /2 …) and its script at an absolute path: at "/index.html" it shows "not found"."""
    folder.mkdir(parents=True, exist_ok=True)
    z = folder / "spa.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("dist/index.html", """<!doctype html><html><head><meta charset="utf-8"></head><body><div id="app"></div><script src="/assets/app.js"></script></body></html>""")
        zf.writestr("dist/assets/app.js", """const T=['One','Two','Three'];function cur(){const m=location.pathname.match(/^\\/(\\d+)$/);return m?+m[1]:0;}
function render(){const c=cur();document.getElementById('app').innerHTML=c?'<h1>'+T[c-1]+'</h1>':'<h1>Not found</h1>';}
if(location.pathname==='/')history.replaceState(null,'','/1');
addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&cur()<3){history.pushState(null,'','/'+(cur()+1));render();}});render();""")
    return z


def auto_turn_deck() -> str:
    """A deck that turns its own pages every 3 seconds."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}</style></head><body>
<section class="slide on" data-notes="{SAY}"><h1>One</h1></section><section class="slide" data-notes="{SAY}"><h1>Two</h1></section>
<section class="slide" data-notes="Three."><h1>Three</h1></section>{TURN}
<script>setInterval(()=>dispatchEvent(new KeyboardEvent('keydown',{{key:'ArrowRight'}})),3000);</script></body></html>"""


def lazy_zip(folder: Path) -> Path:
    """A picture with loading="lazy" on the second slide."""
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (400, 300), (40, 160, 90)).save(folder / "pic.png")
    z = folder / "lazy.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(folder / "pic.png", "pic.png")
        zf.writestr("index.html", f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}img{{width:800px;height:600px}}</style></head><body>
<section class="slide on" data-notes="One."><h1>One</h1></section><section class="slide" data-notes="Two."><img loading="lazy" src="pic.png" alt="p"></section>{TURN}</body></html>""")
    return z


def keys_anim_deck() -> str:
    """The second slide (reached with →) starts a bar that grows over 3 seconds when it shows."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE}
#b{{height:200px;background:#22aa77;width:0}}.on #b{{animation:g 3s linear forwards}}@keyframes g{{to{{width:100%}}}}</style></head><body>
<section class="slide on" data-notes="One."><h1>One</h1></section><section class="slide" data-notes="{SAY}"><div id="b"></div></section>{TURN}</body></html>"""


def remark_like() -> str:
    """Slides of 4:3 scaled into a window of 16:9 (as remark does): a container per slide, the slide itself in a "scaler" inside it."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%;background:#889}
.remark-slide-container{position:absolute;inset:0;display:none;background:#889}.remark-slide-container.remark-visible{display:block}
.remark-slide-scaler{position:absolute;left:240px;top:0;width:1440px;height:1080px;background:#fff}</style></head><body>
<div class="remark-slide-container remark-visible"><div class="remark-slide-scaler"><h1>First</h1></div></div>
<div class="remark-slide-container"><div class="remark-slide-scaler"><h1>Second</h1></div></div>
<script>const S=[...document.querySelectorAll('.remark-slide-container')];let k=0;addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&k<1){S[k].classList.remove('remark-visible');k++;S[k].classList.add('remark-visible');}});</script></body></html>"""


def cards_deck() -> str:
    """Slides with content a narration can name: a grid of cards, a list, a table; a slide title; a button that shows a result."""
    return """<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%;font:28px sans-serif;background:#fff}
.slide{position:absolute;inset:0;padding:60px;box-sizing:border-box;display:none}.slide.on{display:block}
.grid{display:flex;gap:40px;margin-top:40px}.card{width:400px;height:300px;background:#eef3ff;padding:20px;box-sizing:border-box}
#out{height:100px}</style></head><body>
<section class="slide on" data-notes="今天讲三种工具。第一个是扳手，用来拧螺丝。然后是锤子，用来钉钉子。最后说一下卷尺的用法。">
<h2>工具介绍</h2><div class="grid"><div class="card"><h3>扳手</h3><p>拧螺丝</p></div><div class="card"><h3>锤子</h3><p>钉钉子</p></div>
<div class="card"><h3>卷尺</h3><p>量长度</p></div></div></section>
<section class="slide" data-notes="这里有两条规则。第一条是先备份，然后再修改。接下来点一下运行，页面上会出现结果。">
<h2>两条规则</h2><ul><li>先备份</li><li>再发布</li></ul><button id="run" onclick="document.getElementById('out').textContent='已完成处理'">运行</button><div id="out"></div></section>
<script>const S=[...document.querySelectorAll('.slide')];let k=0;addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&k<S.length-1){S[k].classList.remove('on');k++;S[k].classList.add('on');}});</script>
</body></html>"""  # i18n: ignore


def scroll_deck() -> str:
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;font:30px sans-serif}section.slide{height:100vh;padding:60px;box-sizing:border-box;border-bottom:4px solid #ccc}
</style></head><body>
<section class="slide" data-notes="Scroll one."><h1>First screen</h1></section>
<section class="slide" data-notes="Scroll two."><h1>Second screen</h1></section>
<section class="slide" data-notes="Scroll three."><h1>Third screen</h1></section>
</body></html>"""


KEYS_JS = """<script>
const S=[...document.querySelectorAll('.slide')];let k=0;
addEventListener('keydown',e=>{if(e.key==='ArrowRight'&&k<S.length-1){S[k].classList.remove('on');k++;S[k].classList.add('on');}});
</script>"""


def gbk_deck() -> bytes:
    """Saved in GBK without a <meta charset> (as old Chinese editors do); notes on an inner element and in a comment."""
    html = ("""<!doctype html><html><head><title>编码测试</title><style>
body{margin:0;background:#fff}.slide{width:1280px;height:720px;display:none;padding:40px;box-sizing:border-box}.slide.on{display:block}
</style></head><body>
<section class="slide on"><h1>第一页标题</h1><div class="body" data-notes="第一页的讲稿写在里面的元素上。"></div></section>
<section class="slide"><h1>第二页标题</h1><!-- 2 --><p>正文</p><!-- 这是第二页写在注释里的讲稿，讲到这里就够了。 --></section>
""" + KEYS_JS + "</body></html>")
    return html.encode("gbk")


def app_zip(folder: Path) -> Path:
    """A built web app zipped with its folder: the page asks for /assets/… from the site's root."""
    folder.mkdir(parents=True, exist_ok=True)
    z = folder / "app.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("talk/index.html", """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/assets/site.css"></head><body>
<section class="slide on" data-notes="Red title."><h1>Styled</h1></section>
<section class="slide" data-notes="Second."><h1>Two</h1></section>""" + KEYS_JS + "</body></html>")
        zf.writestr("talk/assets/site.css", "body{margin:0}.slide{width:1280px;height:720px;display:none;background:#fff}"
                                             ".slide.on{display:block}h1{color:rgb(200,30,30);font-size:120px;margin:40px}")
    return z


def video_zip(folder: Path) -> Path:
    """One slide playing a short video (made with ffmpeg here) that keeps changing."""
    import subprocess

    from backend.services.ffmpeg_util import ffmpeg_bin
    folder.mkdir(parents=True, exist_ok=True)
    mp4 = folder / "clip.mp4"
    subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-t", "3", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(mp4)], check=True)
    z = folder / "video.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(mp4, "clip.mp4")
        zf.writestr("index.html", """<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0}
.slide{width:1280px;height:720px;background:#fff}</style></head><body>
<section class="slide" data-notes="The video plays while I talk about it for a little while."><h1>Video</h1>
<video src="clip.mp4" autoplay muted loop width="640" height="480"></video></section></body></html>""")
    return z


def pages_zip(folder: Path) -> Path:
    """One page per slide (slide-1.html …) with a picture in img/, zipped as a folder (as Windows' "Send to zip" does)."""
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    pic = folder / "pic.png"
    Image.new("RGB", (200, 120), (40, 160, 90)).save(pic)
    z = folder / "pages.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(pic, "deck/img/pic.png")
        for k in (1, 2, 10):          # natural order: 1, 2, 10
            zf.writestr(f"deck/slide-{k}.html",
                        f"""<!doctype html><html><head><meta charset="utf-8"><style>body{{margin:0;background:#eee}}
.slide{{width:1280px;height:720px;background:#fff;padding:40px;box-sizing:border-box}}</style></head>
<body><div class="slide" data-notes="Page {k} notes."><h1>Page {k}</h1><img src="img/pic.png" alt="pic"></div></body></html>""")
    return z

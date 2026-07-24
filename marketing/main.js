// Vigilist landing page interactions: sticky-nav scroll state + scroll reveals.
'use strict';

const nav = document.querySelector('.nav');
const onScroll = () => nav.classList.toggle('nav--scrolled', window.scrollY > 24);
window.addEventListener('scroll', onScroll, { passive: true });
onScroll();

// Reveal-on-scroll is pure enhancement: content is visible by default, and we
// only opt into the hidden-then-fade-in state when we can actually drive it.
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if (!reduce && 'IntersectionObserver' in window) {
  document.documentElement.classList.add('reveal-anim');
  const io = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('is-visible'); io.unobserve(e.target); } });
  }, { threshold: 0.15 });
  document.querySelectorAll('.reveal').forEach(el => io.observe(el));
}

// Entity graph: hovering a node spotlights its connection.
document.querySelectorAll('.graph__node[data-key]').forEach((node) => {
  const graph = node.closest('.graph');
  if (!graph) return;
  node.addEventListener('mouseenter', () => graph.setAttribute('data-focus', node.dataset.key));
  node.addEventListener('mouseleave', () => graph.removeAttribute('data-focus'));
});

// Demo request modal: blurred backdrop, basic info, delivered by email.
(() => {
  const modal = document.querySelector('.demo');
  if (!modal) return;
  const card = modal.querySelector('.demo__card');
  const form = modal.querySelector('.demo__form');
  const done = modal.querySelector('.demo__done');
  const status = modal.querySelector('.demo__status');
  const submitBtn = modal.querySelector('.demo__submit');
  let lastFocus = null;

  const open = () => {
    lastFocus = document.activeElement;
    modal.hidden = false;
    document.body.classList.add('demo-open');
    const first = form.querySelector('input');
    if (first) first.focus();
  };
  const close = () => {
    modal.hidden = true;
    document.body.classList.remove('demo-open');
    if (lastFocus) lastFocus.focus();
  };

  document.querySelectorAll('[data-demo-open]').forEach(b => b.addEventListener('click', open));
  modal.querySelectorAll('[data-demo-close]').forEach(b => b.addEventListener('click', close));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !modal.hidden) close(); });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    status.textContent = '';
    const data = Object.fromEntries(new FormData(form).entries());
    if (!data.name || !data.email || !/@/.test(data.email)) {
      status.textContent = 'Please add your name and a work email.';
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = 'Sending…';
    try {
      const res = await fetch('https://formsubmit.co/ajax/will@qndary.com', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          _subject: `Vigilist demo request — ${data.name}${data.firm ? ' (' + data.firm + ')' : ''}`,
          name: data.name, email: data.email, firm: data.firm || '', details: data.details || '',
        }),
      });
      if (!res.ok) throw new Error('send failed');
      form.hidden = true;
      done.hidden = false;
      card.querySelector('h2').textContent = 'Request sent.';
    } catch {
      status.textContent = "Couldn't send — please email will@qndary.com directly.";
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Send request';
    }
  });
})();

// Pricing calculator: matter size -> monthly bill + rough document count.
(() => {
  const slider = document.querySelector('#calc-gb');
  if (!slider) return;
  const gbval = document.querySelector('.calc__gbval');
  const price = document.querySelector('.calc__price');
  const docs = document.querySelector('.calc__docs');
  const render = () => {
    const gb = Number(slider.value);
    gbval.textContent = gb;
    price.textContent = '$' + (gb * 18).toLocaleString('en-US');
    docs.textContent = (gb * 5000).toLocaleString('en-US');
  };
  slider.addEventListener('input', render);
  render();
})();

// Feature cards collapse to headings on small screens; tap to expand.
(() => {
  const mq = window.matchMedia('(max-width: 620px)');
  document.querySelectorAll('.grid .card').forEach((card) => {
    card.addEventListener('click', () => {
      if (mq.matches) card.classList.toggle('is-open');
    });
  });
})();

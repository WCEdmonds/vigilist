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

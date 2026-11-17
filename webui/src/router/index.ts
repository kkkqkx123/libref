import { createRouter, createWebHistory } from 'vue-router';
import { messages } from '../i18n';
import { authStore, detectMode, hasScopes } from '../stores/auth';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/search' },
    { path: '/login', component: () => import('../views/LoginView.vue'), meta: { titleKey: 'login', public: true } },
    { path: '/forbidden', component: () => import('../views/ForbiddenView.vue'), meta: { titleKey: 'forbidden', public: true } },
    { path: '/catalog', component: () => import('../views/CatalogView.vue'), meta: { titleKey: 'catalog', scopes: ['read'] } },
    { path: '/search', component: () => import('../views/SearchView.vue'), meta: { titleKey: 'search', scopes: ['read'] } },
    { path: '/answer', component: () => import('../views/AnswerView.vue'), meta: { titleKey: 'answer', scopes: ['read'] } },
    { path: '/reader', component: () => import('../views/ReaderView.vue'), meta: { titleKey: 'reader', scopes: ['read'] } },
    { path: '/insights/api', component: () => import('../views/ApiInsightView.vue'), meta: { titleKey: 'apiInsight', scopes: ['read'] } },
    { path: '/insights/calls', component: () => import('../views/CallInsightView.vue'), meta: { titleKey: 'calls', scopes: ['read'] } },
    { path: '/insights/symbols', component: () => import('../views/SymbolInsightView.vue'), meta: { titleKey: 'symbols', scopes: ['read'] } },
    { path: '/insights/imports', component: () => import('../views/ImportInsightView.vue'), meta: { titleKey: 'imports', scopes: ['read'] } },
    { path: '/insights/patterns', component: () => import('../views/PatternInsightView.vue'), meta: { titleKey: 'patterns', scopes: ['read'] } },
    { path: '/ingest', component: () => import('../views/IngestWizardView.vue'), meta: { titleKey: 'ingest', scopes: ['ingest:write'] } },
    { path: '/jobs', component: () => import('../views/JobsView.vue'), meta: { titleKey: 'jobs', scopes: ['read'] } },
    { path: '/review', component: () => import('../views/ReviewView.vue'), meta: { titleKey: 'review', scopes: ['review:read'] } },
    { path: '/feedback', component: () => import('../views/FeedbackView.vue'), meta: { titleKey: 'feedback', scopes: ['feedback:read', 'feedback:write'], any: true } },
    { path: '/admin', component: () => import('../views/AdminView.vue'), meta: { titleKey: 'admin', scopes: ['admin:keys', 'admin:config'], any: true } },
    { path: '/:pathMatch(.*)*', redirect: '/search' },
  ],
});

router.beforeEach(async (to) => {
  await detectMode();
  if (authStore.mode === 'enforced' && !authStore.key && !to.meta.public) {
    return { path: '/login', query: { redirect: to.fullPath } };
  }
  const scopes = (to.meta.scopes as string[] | undefined) || [];
  if (scopes.length > 0) {
    const any = to.meta.any === true;
    const ok = any
      ? scopes.some((scope) => (authStore.me?.scopes || []).includes(scope))
      : hasScopes(scopes);
    if (!ok && authStore.me) return { path: '/forbidden' };
    if (!ok) return { path: '/login', query: { redirect: to.fullPath } };
  }
  const key = to.meta.titleKey as string | undefined;
  const title = key && key in messages.value.routes ? messages.value.routes[key as keyof typeof messages.value.routes] : 'libref';
  document.title = `${title} - libref`;
  return true;
});

export default router;

import { createRouter, createWebHistory } from 'vue-router';
import { authStore, detectMode, hasScopes } from '../stores/auth';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/search' },
    { path: '/login', component: () => import('../views/LoginView.vue'), meta: { title: '登录', public: true } },
    { path: '/forbidden', component: () => import('../views/ForbiddenView.vue'), meta: { title: '无权限', public: true } },
    { path: '/catalog', component: () => import('../views/CatalogView.vue'), meta: { title: '文库目录', scopes: ['read'] } },
    { path: '/search', component: () => import('../views/SearchView.vue'), meta: { title: '搜索', scopes: ['read'] } },
    { path: '/answer', component: () => import('../views/AnswerView.vue'), meta: { title: '问答', scopes: ['read'] } },
    { path: '/reader', component: () => import('../views/ReaderView.vue'), meta: { title: '文档', scopes: ['read'] } },
    { path: '/insights/api', component: () => import('../views/ApiInsightView.vue'), meta: { title: 'API 洞察', scopes: ['read'] } },
    { path: '/insights/calls', component: () => import('../views/CallInsightView.vue'), meta: { title: '调用', scopes: ['read'] } },
    { path: '/insights/symbols', component: () => import('../views/SymbolInsightView.vue'), meta: { title: '符号', scopes: ['read'] } },
    { path: '/insights/imports', component: () => import('../views/ImportInsightView.vue'), meta: { title: '导入', scopes: ['read'] } },
    { path: '/insights/patterns', component: () => import('../views/PatternInsightView.vue'), meta: { title: '模式', scopes: ['read'] } },
    { path: '/ingest', component: () => import('../views/IngestWizardView.vue'), meta: { title: '入库向导', scopes: ['ingest:write'] } },
    { path: '/jobs', component: () => import('../views/JobsView.vue'), meta: { title: '任务中心', scopes: ['read'] } },
    { path: '/review', component: () => import('../views/ReviewView.vue'), meta: { title: '审核', scopes: ['review:read'] } },
    { path: '/feedback', component: () => import('../views/FeedbackView.vue'), meta: { title: '反馈', scopes: ['feedback:read', 'feedback:write'], any: true } },
    { path: '/admin', component: () => import('../views/AdminView.vue'), meta: { title: '管理', scopes: ['admin:keys', 'admin:config'], any: true } },
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
  document.title = `${String(to.meta.title || 'libref')} - libref`;
  return true;
});

export default router;

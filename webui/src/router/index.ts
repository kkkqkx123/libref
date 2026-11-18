import { createRouter, createWebHistory } from 'vue-router';
import type { RouteRecordRaw } from 'vue-router';
import { watch } from 'vue';
import { locale, messages } from '../i18n';
import { detectMode } from '../stores/auth';
import { authGuard } from './guard';
import { NAV_ROUTES } from './nav';

const navRoutes: RouteRecordRaw[] = NAV_ROUTES.map((entry) => ({
  ...entry.route,
  meta: {
    titleKey: entry.titleKey,
    scopes: entry.scopes,
    any: entry.any === true,
  },
}));

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/search' },
    { path: '/login', component: () => import('../views/LoginView.vue'), meta: { titleKey: 'login', public: true } },
    { path: '/forbidden', component: () => import('../views/ForbiddenView.vue'), meta: { titleKey: 'forbidden', public: true } },
    ...navRoutes,
    { path: '/:pathMatch(.*)*', redirect: '/search' },
  ],
});

router.beforeEach(async (to) => {
  await detectMode();
  return authGuard(to);
});

function applyTitle(route = router.currentRoute.value): void {
  const key = route.meta.titleKey as string | undefined;
  const routes = messages.value.routes;
  const title = key && key in routes ? routes[key as keyof typeof routes] : messages.value.app.brand;
  document.title = `${title} - ${messages.value.app.brand}`;
}

router.afterEach((to) => {
  applyTitle(to);
});

// Switching language keeps the route, so the title has to be rewritten here.
watch(locale, () => {
  applyTitle();
});

export default router;

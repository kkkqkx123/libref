// Single table for navigable pages: the router and the sidebar both derive from
// it, so route metadata and menu visibility cannot drift apart.
import type { RouteRecordRaw } from 'vue-router';
import { messages } from '../i18n';

export type NavGroupKey = 'retrieval' | 'insights' | 'ingestTasks' | 'governance';

export type NavTitleKey = keyof (typeof messages.value)['routes'] &
  keyof (typeof messages.value)['nav'];

export interface NavRoute {
  route: RouteRecordRaw;
  titleKey: NavTitleKey;
  scopes: string[];
  any?: boolean;
  // Entries without a group stay reachable by URL but never enter the sidebar.
  group?: NavGroupKey;
}

export const NAV_ROUTES: NavRoute[] = [
  {
    route: {
      path: '/catalog',
      component: () => import('../views/CatalogView.vue'),
    },
    titleKey: 'catalog',
    scopes: ['read'],
    group: 'retrieval',
  },
  {
    route: {
      path: '/search',
      component: () => import('../views/SearchView.vue'),
    },
    titleKey: 'search',
    scopes: ['read'],
    group: 'retrieval',
  },
  {
    route: {
      path: '/answer',
      component: () => import('../views/AnswerView.vue'),
    },
    titleKey: 'answer',
    scopes: ['read'],
    group: 'retrieval',
  },
  {
    route: {
      path: '/reader',
      component: () => import('../views/ReaderView.vue'),
    },
    titleKey: 'reader',
    scopes: ['read'],
  },
  {
    route: {
      path: '/insights/api',
      component: () => import('../views/ApiInsightView.vue'),
    },
    titleKey: 'apiInsight',
    scopes: ['read'],
    group: 'insights',
  },
  {
    route: {
      path: '/insights/calls',
      component: () => import('../views/CallInsightView.vue'),
    },
    titleKey: 'calls',
    scopes: ['read'],
    group: 'insights',
  },
  {
    route: {
      path: '/insights/symbols',
      component: () => import('../views/SymbolInsightView.vue'),
    },
    titleKey: 'symbols',
    scopes: ['read'],
    group: 'insights',
  },
  {
    route: {
      path: '/insights/imports',
      component: () => import('../views/ImportInsightView.vue'),
    },
    titleKey: 'imports',
    scopes: ['read'],
    group: 'insights',
  },
  {
    route: {
      path: '/insights/patterns',
      component: () => import('../views/PatternInsightView.vue'),
    },
    titleKey: 'patterns',
    scopes: ['read'],
    group: 'insights',
  },
  {
    route: {
      path: '/insights/doc',
      component: () => import('../views/DocInsightView.vue'),
    },
    titleKey: 'docInsight',
    scopes: ['read'],
    group: 'insights',
  },
  {
    route: {
      path: '/ingest',
      component: () => import('../views/IngestWizardView.vue'),
    },
    titleKey: 'ingest',
    scopes: ['ingest:write'],
    group: 'ingestTasks',
  },
  {
    route: {
      path: '/requests',
      component: () => import('../views/RequestsView.vue'),
    },
    titleKey: 'requests',
    scopes: ['index:request'],
    group: 'ingestTasks',
  },
  {
    route: {
      path: '/jobs',
      component: () => import('../views/JobsView.vue'),
    },
    titleKey: 'jobs',
    scopes: ['read'],
    group: 'ingestTasks',
  },
  {
    route: {
      path: '/requests/approvals',
      component: () => import('../views/RequestApprovalsView.vue'),
    },
    titleKey: 'requestApprovals',
    scopes: ['index:approve'],
    group: 'governance',
  },
  {
    route: {
      path: '/review',
      component: () => import('../views/ReviewView.vue'),
    },
    titleKey: 'review',
    scopes: ['review:read'],
    group: 'governance',
  },
  {
    route: {
      path: '/feedback',
      component: () => import('../views/FeedbackView.vue'),
    },
    titleKey: 'feedback',
    scopes: ['feedback:read', 'feedback:read:own', 'feedback:write'],
    any: true,
    group: 'governance',
  },
  {
    route: {
      path: '/admin',
      component: () => import('../views/AdminView.vue'),
    },
    titleKey: 'admin',
    scopes: ['admin:keys', 'admin:config'],
    any: true,
    group: 'governance',
  },
];

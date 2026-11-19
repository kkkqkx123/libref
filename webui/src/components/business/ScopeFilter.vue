<template>
  <el-form :inline="true" class="libref-scope-filter" @submit.prevent>
    <el-form-item :label="messages.common.library">
      <el-select
        v-model="scopeStore.library"
        filterable
        clearable
        :placeholder="messages.common.allLibraries"
        @change="onLibraryChange" class="libref-w-200">
        <el-option
          v-for="library in libraries"
          :key="`${library.ecosystem}/${library.name}`"
          :label="`${library.name}（${library.ecosystem}）`"
          :value="library.name"
        />
      </el-select>
    </el-form-item>
    <el-form-item :label="messages.common.version">
      <el-select
        v-model="scopeStore.version"
        clearable
        :placeholder="messages.common.allVersions" class="libref-w-160">
        <el-option
          v-for="version in versions"
          :key="version.version"
          :label="version.version"
          :value="version.version"
        />
      </el-select>
    </el-form-item>
    <el-form-item :label="messages.common.ecosystem">
      <el-input v-model="scopeStore.ecosystem" clearable placeholder="npm" class="libref-w-140"/>
    </el-form-item>
    <el-form-item v-if="showPreset" :label="messages.common.preset">
      <el-radio-group v-model="scopeStore.preset">
        <el-radio-button value="precise">{{ messages.common.precise }}</el-radio-button>
        <el-radio-button value="balanced">{{ messages.common.balanced }}</el-radio-button>
        <el-radio-button value="thorough">{{ messages.common.thorough }}</el-radio-button>
      </el-radio-group>
    </el-form-item>
    <el-form-item v-if="showLimit" :label="messages.common.limit">
      <el-input-number
        v-model="scopeStore.resultLimit"
        :min="RESULT_LIMIT_MIN"
        :max="RESULT_LIMIT_MAX" class="libref-w-120"/>
    </el-form-item>
  </el-form>
</template>

<script setup lang="ts">
import { ElForm, ElFormItem, ElInput, ElInputNumber, ElOption, ElRadioButton, ElRadioGroup, ElSelect } from 'element-plus';
import { computed, onMounted, watch } from 'vue';
import { api } from '../../api/client';
import type { LibraryRow, VersionRow } from '../../api/types';
import { catalogStore } from '../../stores/catalog';
import { notifyError } from '../../stores/notify';
import { RESULT_LIMIT_MAX, RESULT_LIMIT_MIN, scopeStore, setLibrary } from '../../stores/scope';
import { messages } from '../../i18n';

withDefaults(defineProps<{ showPreset?: boolean; showLimit?: boolean }>(), {
  showPreset: false,
  showLimit: false,
});

const libraries = computed(() => catalogStore.libraries);
const versions = computed<VersionRow[]>(() => catalogStore.versionsByLibrary[scopeStore.library] || []);

async function ensureLibraries(): Promise<void> {
  if (catalogStore.loaded) return;
  try {
    const { data } = await api('/catalog');
    catalogStore.libraries = data.items as LibraryRow[];
    catalogStore.loaded = true;
  } catch (error) {
    notifyError(error, messages.value.errors.loadCatalogFailed);
  }
}

async function ensureVersions(): Promise<void> {
  if (!scopeStore.library) return;
  if (catalogStore.versionsByLibrary[scopeStore.library]) return;
  try {
    const { data } = await api('/catalog', {
      params: { library: scopeStore.library, ecosystem: scopeStore.ecosystem || undefined },
    });
    catalogStore.versionsByLibrary[scopeStore.library] = data.items as VersionRow[];
  } catch (error) {
    notifyError(error, messages.value.errors.loadVersionsFailed);
  }
}

function onLibraryChange(): void {
  setLibrary(scopeStore.library);
  void ensureVersions();
}

onMounted(() => {
  void ensureLibraries().then(() => ensureVersions());
});

watch(
  () => scopeStore.library,
  () => {
    void ensureVersions();
  },
);
</script>

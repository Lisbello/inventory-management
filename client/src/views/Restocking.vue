<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div class="card budget-card">
      <div class="budget-header">
        <label for="budget-slider" class="budget-label">{{ t('restocking.budget') }}</label>
        <span class="budget-value">{{ money(budget) }}</span>
      </div>
      <input
        id="budget-slider"
        v-model.number="budget"
        type="range"
        class="budget-slider"
        :min="0"
        :max="MAX_BUDGET"
        :step="STEP"
        :aria-label="t('restocking.budgetSlider')"
      />
      <div class="slider-scale">
        <span>{{ money(0) }}</span>
        <span>{{ money(MAX_BUDGET) }}</span>
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat-card info">
        <div class="stat-label">{{ t('restocking.budget') }}</div>
        <div class="stat-value">{{ money(budget) }}</div>
      </div>
      <div class="stat-card success">
        <div class="stat-label">{{ t('restocking.recommendedTotal') }}</div>
        <div class="stat-value">{{ money(totalCost, 2) }}</div>
      </div>
      <div class="stat-card warning">
        <div class="stat-label">{{ t('restocking.remaining') }}</div>
        <div class="stat-value">{{ money(remaining, 2) }}</div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3 class="card-title">{{ t('restocking.recommendedItems') }} ({{ items.length }})</h3>
        <button
          type="button"
          class="place-order-btn"
          :disabled="items.length === 0 || submitting || loading"
          @click="placeOrder"
        >
          {{ submitting ? t('restocking.submitting') : t('restocking.placeOrder') }}
        </button>
      </div>

      <div v-if="orderSuccess" class="message success-message" role="status">{{ orderSuccess }}</div>
      <div v-if="orderError" class="error" role="alert">{{ orderError }}</div>
      <div v-if="error" class="error" role="alert">{{ error }}</div>

      <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
      <div v-else-if="items.length === 0" class="empty-state">
        {{ budget === 0 ? t('restocking.emptyZero') : t('restocking.emptyNoFit') }}
      </div>
      <div v-else class="table-container">
        <table>
          <thead>
            <tr>
              <th>{{ t('restocking.table.sku') }}</th>
              <th>{{ t('restocking.table.name') }}</th>
              <th>{{ t('restocking.table.trend') }}</th>
              <th class="num">{{ t('restocking.table.demandGap') }}</th>
              <th class="num">{{ t('restocking.table.quantity') }}</th>
              <th class="num">{{ t('restocking.table.unitCost') }}</th>
              <th class="num">{{ t('restocking.table.lineTotal') }}</th>
              <th class="num">{{ t('restocking.table.leadTime') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in items" :key="item.item_sku">
              <td><strong>{{ item.item_sku }}</strong></td>
              <td>{{ translateProductName(item.item_name) }}</td>
              <td>
                <span :class="['badge', item.trend]">{{ trendLabel(item.trend) }}</span>
              </td>
              <td class="num">{{ item.demand_gap.toLocaleString() }}</td>
              <td class="num">{{ item.recommended_quantity.toLocaleString() }}</td>
              <td class="num">{{ money(item.unit_cost, 2) }}</td>
              <td class="num"><strong>{{ money(item.line_total, 2) }}</strong></td>
              <td class="num">{{ item.lead_time_days }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrencyWithDecimals } from '../utils/currency'

const { t, currentCurrency, currentLocale, translateProductName } = useI18n()

// Slider bounds (USD); full demand-gap cost is roughly $9,900
const MAX_BUDGET = 15000
const STEP = 100
const DEBOUNCE_MS = 300

const budget = ref(5000)
const items = ref([])
const totalCost = ref(0)
const remainingBudget = ref(0)
const loading = ref(false)
const error = ref(null)
const submitting = ref(false)
const orderSuccess = ref(null)
const orderError = ref(null)

// Between slider moves and the debounced response, show the live budget minus the last total
const remaining = computed(() => Math.max(budget.value - totalCost.value, 0))

const money = (amount, decimals = 0) =>
  formatCurrencyWithDecimals(amount, currentCurrency.value, decimals)

const trendLabel = (trend) => {
  const key = `restocking.trend.${trend}`
  const label = t(key)
  return label === key ? trend : label
}

const formatDate = (value) => {
  const date = new Date(value)
  if (isNaN(date.getTime())) return value
  return date.toLocaleDateString(currentLocale.value === 'ja' ? 'ja-JP' : 'en-US', {
    year: 'numeric', month: 'short', day: 'numeric'
  })
}

// Monotonic id so a slow, older response can never overwrite a newer one
let requestId = 0

const loadRecommendations = async () => {
  const id = ++requestId
  error.value = null
  if (budget.value <= 0) {
    // Budget 0: skip the API call and show the empty state
    items.value = []
    totalCost.value = 0
    remainingBudget.value = 0
    loading.value = false
    return
  }
  loading.value = true
  try {
    const data = await api.getRestockRecommendations(budget.value)
    if (id !== requestId) return
    items.value = data.items
    totalCost.value = data.total_cost
    remainingBudget.value = data.remaining_budget
  } catch (err) {
    if (id !== requestId) return
    error.value = t('restocking.loadFailed')
    items.value = []
    totalCost.value = 0
    remainingBudget.value = 0
    console.error('Restock recommendations error:', err)
  } finally {
    if (id === requestId) loading.value = false
  }
}

let debounceTimer = null
watch(budget, () => {
  orderSuccess.value = null
  orderError.value = null
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(loadRecommendations, DEBOUNCE_MS)
})

const placeOrder = async () => {
  if (items.value.length === 0 || submitting.value) return
  submitting.value = true
  orderSuccess.value = null
  orderError.value = null
  try {
    const order = await api.createRestockOrder({
      budget: budget.value,
      items: items.value.map(i => ({ item_sku: i.item_sku, quantity: i.recommended_quantity }))
    })
    orderSuccess.value = t('restocking.success', {
      orderNumber: order.order_number,
      date: formatDate(order.expected_delivery_date)
    })
  } catch (err) {
    orderError.value = err.response?.data?.detail || t('restocking.failed')
  } finally {
    submitting.value = false
  }
}

onMounted(loadRecommendations)
onBeforeUnmount(() => clearTimeout(debounceTimer))
</script>

<style scoped>
.budget-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.75rem;
}

.budget-label {
  font-size: 0.875rem;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.025em;
}

.budget-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-slider {
  width: 100%;
  accent-color: #3b82f6;
  cursor: pointer;
}

.slider-scale {
  display: flex;
  justify-content: space-between;
  font-size: 0.75rem;
  color: #64748b;
  margin-top: 0.25rem;
}

.place-order-btn {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 8px;
  padding: 0.5rem 1.25rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s ease;
}

.place-order-btn:hover:not(:disabled) {
  background: #1e293b;
}

.place-order-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.message {
  padding: 1rem;
  border-radius: 8px;
  margin: 0 0 1rem;
  font-size: 0.938rem;
}

.success-message {
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  color: #166534;
}

.empty-state {
  text-align: center;
  padding: 2.5rem 1rem;
  color: #64748b;
  font-size: 0.938rem;
}

.num {
  text-align: right;
}
</style>

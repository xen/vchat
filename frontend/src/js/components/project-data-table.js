import {
  createTable,
  getCoreRowModel,
  getPaginationRowModel,
  getFilteredRowModel,
  getSortedRowModel,
} from '@tanstack/table-core'

const flexRender = (comp, props) => {
  if (typeof comp === "function") {
    return comp(props)
  }
  return comp
}

const cloneStateSlice = (value) => {
  if (Array.isArray(value)) {
    return value.map(item => cloneStateSlice(item))
  }
  if (value && typeof value === 'object') {
    return Object.entries(value).reduce((acc, [key, val]) => {
      acc[key] = cloneStateSlice(val)
      return acc
    }, {})
  }
  return value
}

const escapeHtml = (value) => {
  if (value === null || value === undefined) {
    return ''
  }
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const formatBytes = (value) => {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return '0 B'
  }

  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  )
  const quotient = bytes / 1024 ** exponent

  let formatted
  if (quotient >= 100) {
    formatted = Math.round(quotient).toString()
  } else if (quotient >= 10) {
    formatted = (Math.round(quotient * 10) / 10).toString()
  } else {
    formatted = (Math.round(quotient * 100) / 100).toString()
  }

  return `${formatted} ${units[exponent]}`
}

const DOCUMENT_TYPE_META = {
  html: { label: 'HTML-документ', icon: 'lucide:globe' },
  markdown: { label: 'Markdown-документ', icon: 'lucide:file-text' },
  office: { label: 'Офисный документ', icon: 'lucide:file-spreadsheet' },
  audio: { label: 'Аудиофайл', icon: 'lucide:file-music' },
  video: { label: 'Видеофайл', icon: 'lucide:file-video' },
  code: { label: 'Исходный код', icon: 'lucide:file-code' },
  pdf: { label: 'PDF-документ', icon: 'lucide:file-text' },
  other: { label: 'Другое', icon: 'lucide:file' },
}

const resolveDocumentType = (value) => {
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (trimmed) {
      return trimmed
    }
  }
  return 'other'
}

const formatDocumentStatus = (value) => {
  const normalized = typeof value === 'string' ? value.trim().toLowerCase() : ''
  if (normalized === 'indexed') {
    return {
      label: 'Индексирован',
      className: 'badge badge-success badge-sm badge-soft',
    }
  }
  if (normalized === 'added') {
    return {
      label: 'В очереди',
      className: 'badge badge-warning badge-sm badge-soft',
    }
  }
  return {
    label: normalized || 'unknown',
    className: 'badge badge-outline badge-sm',
  }
}

const compareIsoDates = (rowA, rowB, columnId) => {
  const toTimestamp = (value) => {
    if (!value) return 0
    const ts = new Date(value).getTime()
    return Number.isFinite(ts) ? ts : 0
  }
  return toTimestamp(rowA.getValue(columnId)) - toTimestamp(rowB.getValue(columnId))
}

const compareNumbers = (rowA, rowB, columnId) => {
  const a = Number(rowA.getValue(columnId)) || 0
  const b = Number(rowB.getValue(columnId)) || 0
  return a - b
}

const compareStrings = (rowA, rowB, columnId) => {
  const a = (rowA.getValue(columnId) ?? '').toString().toLowerCase()
  const b = (rowB.getValue(columnId) ?? '').toString().toLowerCase()
  return a.localeCompare(b)
}

const DOCUMENT_REFRESH_EVENT = "project-documents:refresh"

window.useProjectDataTable = () => {
  const sortableHeader = (label) => ({ column }) => {
    const title = escapeHtml(label)
    if (!column.getCanSort?.()) {
      return `<span class="text-xs text-base-content/70">${title}</span>`
    }

    const sortState = column.getIsSorted?.()
    let icon = '&uarr;&darr;'
    if (sortState === 'asc') {
      icon = '&uarr;'
    } else if (sortState === 'desc') {
      icon = '&darr;'
    }

    return `
            <button
                type="button"
                class="flex items-center gap-1 text-xs text-base-content/70"
                data-sort-column="${escapeHtml(column.id)}"
                aria-label="Сортировать по: ${title}"
            >
                <span>${title}</span>
                <span class="text-[10px] text-base-content/40">${icon}</span>
            </button>
        `
  }

  const formatShortDate = (isoStr) => {
    if (!isoStr) return ''
    const d = new Date(isoStr)
    if (!Number.isFinite(d.getTime())) return ''
    const day = d.getDate().toString().padStart(2, '0')
    const month = (d.getMonth() + 1).toString().padStart(2, '0')
    const year = d.getFullYear().toString().slice(2)
    return `${day}.${month}.${year}`
  }

  const statusDotClass = (status, chunkCount) => {
    if (Number(chunkCount) > 0) return 'inline-block size-1.5 rounded-full bg-success shrink-0'
    const s = typeof status === 'string' ? status.trim().toLowerCase() : ''
    if (s === 'added' || s === 'indexed') return 'inline-block size-1.5 rounded-full bg-warning shrink-0'
    return 'inline-block size-1.5 rounded-full bg-base-300 shrink-0'
  }

  const columns = [
    {
      id: "actions",
      header: "",
      meta: { thStyle: 'width:80px', tdStyle: 'padding-right:4px' },
      cell: (info) => {
        const docId = info.row.original.id
        const isIgnored = Boolean(info.row.original.is_ignored)
        const toggleTitle = isIgnored ? "Вернуть в индекс" : "Игнорировать"
        const toggleValue = isIgnored ? "false" : "true"
        const toggleIcon = isIgnored ? "lucide:eye" : "lucide:eye-off"
        const toggleClass = isIgnored
          ? "btn btn-ghost btn-xs px-1 text-success"
          : "btn btn-ghost btn-xs px-1 text-base-content/40"

        return `
          <div class="flex items-center">
            <button type="button" class="btn btn-ghost btn-xs px-1"
              data-doc-action="view" data-doc-id="${docId}" title="Открыть содержимое">
              <iconify-icon icon="lucide:eye" class="size-3.5"></iconify-icon>
            </button>
            <button type="button" class="${toggleClass}"
              data-doc-action="toggle-ignore" data-doc-id="${docId}"
              data-doc-ignore-value="${toggleValue}" title="${toggleTitle}">
              <iconify-icon icon="${toggleIcon}" class="size-3.5"></iconify-icon>
            </button>
            <button type="button" class="btn btn-ghost btn-xs px-1 text-error/50 hover:text-error"
              data-doc-action="delete" data-doc-id="${docId}" title="Удалить">
              <iconify-icon icon="lucide:trash-2" class="size-3.5"></iconify-icon>
            </button>
          </div>`
      },
      enableSorting: false,
    },
    {
      accessorKey: "title",
      header: sortableHeader("Название"),
      cell: (info) => {
        const rawTitle = info.getValue()
        const title = escapeHtml((rawTitle || '').trim() || '[Без названия]')
        const docId = escapeHtml(info.row.original.id)
        const uri = escapeHtml(info.row.original.uri || '')
        const dateStr = escapeHtml(formatShortDate(info.row.original.created_at))
        const dotClass = statusDotClass(info.row.original.status, info.row.original.chunk_count)

        return `
          <div class="overflow-hidden">
            <a class="font-medium text-sm link link-hover block truncate"
              href="/document/${docId}" title="${title}">${title}</a>
            <div class="flex items-center gap-1.5 text-xs text-base-content/40 mt-0.5">
              ${uri ? `<a href="${uri}" target="_blank" rel="noopener" class="truncate min-w-0 hover:text-base-content/70 transition-colors">${uri}</a>` : ''}
              <span class="shrink-0 flex items-center gap-1 ml-auto pl-2">
                <span class="${dotClass}" title="${escapeHtml(info.row.original.status || '')}"></span>
                ${dateStr}
              </span>
            </div>
          </div>`
      },
      enableSorting: true,
    },
    {
      accessorKey: "source",
      header: "",
      cell: () => '',
      enableSorting: false,
      filterFn: (row, columnId, filterValue) => {
        if (!filterValue) return true
        return (row.getValue(columnId) ?? '').toString() === filterValue
      },
    },
    {
      id: "size_chunks",
      header: sortableHeader("Размер"),
      meta: { thStyle: 'width:120px', tdStyle: 'padding-left:12px' },
      accessorFn: (row) => Number(row?.size_bytes ?? 0),
      cell: (info) => {
        const bytes = Number(info.row.original.size_bytes || 0)
        const chunkCount = Number(info.row.original.chunk_count || 0)
        const human = escapeHtml(formatBytes(bytes))
        const chunkLabel = escapeHtml(`${chunkCount} чанк${chunkCount === 1 ? '' : chunkCount >= 2 && chunkCount <= 4 ? 'а' : 'ов'}`)
        return `
          <div class="text-xs whitespace-nowrap">
            <div class="font-medium">${human}</div>
            <div class="opacity-50">${chunkLabel}</div>
          </div>`
      },
      enableSorting: true,
      sortingFn: compareNumbers,
    },
  ]

  return {
    flexRender,
    loading: true,
    headerGroups: [],
    visibleRows: [],
    filteredRowCount: 0,
    pageIndex: 0,
    pageSize: 10,
    pageCount: 0,
    canPreviousPage: false,
    canNextPage: false,
    rangeStart: 0,
    rangeEnd: 0,
    search: "",
    sourceFilter: "",
    table: null,
    state: null,
    data: [],
    refreshListener: null,
    actionListener: null,
    headerListener: null,
    csrfToken: null,
    documentModal: null,
    documentModalContent: null,
    init() {
      this.state = {
        pagination: {
          pageSize: 10,
          pageIndex: 0,
        },
        globalFilter: "",
        sorting: [],
        columnFilters: [],
        columnVisibility: { source: false },
      }

      this.initFromUrl()

      const component = this

      this.table = createTable({
        state: this.state,
        data: this.data,
        columns,
        getCoreRowModel: getCoreRowModel(),
        getPaginationRowModel: getPaginationRowModel(),
        getFilteredRowModel: getFilteredRowModel(),
        getSortedRowModel: getSortedRowModel(),
        autoResetPageIndex: false,
        onStateChange: (updater) => {
          const newState = typeof updater === "function" ? updater(component.state) : updater
          Object.assign(component.state, newState)
          component.updateDerivedState()
          component.updateUrl()
        },
      })

      Object.entries(this.table.initialState).forEach(([key, value]) => {
        if (this.state[key] === undefined) {
          this.state[key] = cloneStateSlice(value)
        }
      })

      this.csrfToken = this.extractCsrfToken()
      this.$nextTick?.(() => this.setupActionDelegation())
      this.$nextTick?.(() => this.setupHeaderDelegation())
      this.registerRefreshListener()
      this.registerPopStateListener()
      this.updateDerivedState()
      this.fetchDocuments()
    },
    initFromUrl() {
      if (typeof window === 'undefined') return
      const params = new URLSearchParams(window.location.search)
      const search = params.get('search')
      const source = params.get('source')
      const page = params.get('page')

      if (search) {
        this.state.globalFilter = search
        this.search = search
      }
      if (source) {
        this.sourceFilter = source
        this.state.columnFilters = [{ id: 'source', value: source }]
      } else {
        this.sourceFilter = ""
        this.state.columnFilters = []
      }
      if (page) {
        const pageIndex = parseInt(page, 10) - 1
        if (!isNaN(pageIndex) && pageIndex >= 0) {
          this.state.pagination.pageIndex = pageIndex
        }
      }
    },
    updateUrl() {
      if (typeof window === 'undefined') return
      const url = new URL(window.location)
      const params = url.searchParams

      if (this.state.globalFilter) {
        params.set('search', this.state.globalFilter)
      } else {
        params.delete('search')
      }

      if (this.sourceFilter) {
        params.set('source', this.sourceFilter)
      } else {
        params.delete('source')
      }

      if (this.state.pagination.pageIndex > 0) {
        params.set('page', this.state.pagination.pageIndex + 1)
      } else {
        params.delete('page')
      }

      const newUrl = url.toString()
      if (newUrl !== window.location.href) {
        window.history.pushState({}, '', newUrl)
      }
    },
    registerPopStateListener() {
      if (typeof window === 'undefined') return
      window.addEventListener('popstate', () => {
        this.initFromUrl()
        this.table.setOptions(prev => ({
          ...prev,
          state: {
            ...prev.state,
            pagination: {
              ...prev.state.pagination,
              pageIndex: this.state.pagination.pageIndex
            },
            globalFilter: this.state.globalFilter
            ,
            columnFilters: this.state.columnFilters
          }
        }))
        this.updateDerivedState()
      })
    },
    fetchDocuments() {
      this.loading = true
      fetch('/documents/json')
        .then(res => res.json())
        .then(jsonData => {
          this.data = Array.isArray(jsonData)
            ? jsonData.map(item => {
              return {
                ...(item || {}),
                document_type: resolveDocumentType(item?.document_type),
                size_bytes: Number(item?.size_bytes ?? 0),
                chunk_count: Number(item?.chunk_count ?? 0),
              }
            })
            : []
          this.table.setOptions(prev => ({ ...prev, data: this.data }))
          this.loading = false
          this.updateDerivedState()
        })
        .catch(err => {
          console.error('[ProjectDataTable] Failed to load documents:', err)
          this.loading = false
          this.updateDerivedState()
        })
    },
    updateDerivedState() {
      if (!this.table) return
      this.headerGroups = [...this.table.getHeaderGroups()]
      this.visibleRows = [...this.table.getRowModel().rows]
      this.filteredRowCount = this.table.getFilteredRowModel().rows.length
      this.pageCount = this.table.getPageCount()
      this.canPreviousPage = this.table.getCanPreviousPage()
      this.canNextPage = this.table.getCanNextPage()
      this.pageIndex = this.state.pagination.pageIndex
      this.pageSize = this.state.pagination.pageSize

      if (this.filteredRowCount === 0) {
        this.rangeStart = 0
        this.rangeEnd = 0
      } else {
        this.rangeStart = this.pageIndex * this.pageSize + 1
        this.rangeEnd = Math.min(
          (this.pageIndex + 1) * this.pageSize,
          this.filteredRowCount
        )
      }
    },
    nextPage() {
      this.table?.nextPage()
    },
    prevPage() {
      this.table?.previousPage()
    },
    setPageIndex(index) {
      this.table?.setPageIndex(index)
    },
    updateSearch() {
      this.table?.setPageIndex(0)
      this.table?.setGlobalFilter(this.search)
    },
    updateSourceFilter() {
      this.table?.setPageIndex(0)
      const sourceColumn = this.table?.getColumn('source')
      if (!sourceColumn) {
        return
      }
      sourceColumn.setFilterValue(this.sourceFilter || undefined)
    },
    registerRefreshListener() {
      if (this.refreshListener || typeof document === 'undefined') {
        return
      }
      const component = this
      this.refreshListener = () => component.fetchDocuments()
      document.addEventListener(DOCUMENT_REFRESH_EVENT, this.refreshListener)
    },
    emitRefreshEvent() {
      if (typeof document === 'undefined') {
        return
      }
      document.dispatchEvent(new CustomEvent(DOCUMENT_REFRESH_EVENT))
    },
    extractCsrfToken() {
      if (typeof document === 'undefined') {
        return null
      }
      const attr = document.body?.getAttribute('hx-headers')
      if (!attr) {
        return null
      }
      try {
        const parsed = JSON.parse(attr)
        return parsed?.["X-CSRFToken"] || null
      } catch {
        return null
      }
    },
    setupActionDelegation() {
      if (this.actionListener) {
        return
      }
      const body = this.$refs?.documentsBody
      if (!body) {
        return
      }
      this.actionListener = (event) => {
        const target = event.target.closest('[data-doc-action]')
        if (!target) return
        const docId = target.dataset.docId
        if (!docId) return

        if (target.dataset.docAction === 'toggle-ignore') {
          const desired = target.dataset.docIgnoreValue === "true"
          this.toggleIgnore(docId, desired)
        } else if (target.dataset.docAction === 'delete') {
          this.deleteDocument(docId)
        } else if (target.dataset.docAction === 'view') {
          this.viewDocument(docId)
        }
      }
      body.addEventListener('click', this.actionListener)
    },
    setupHeaderDelegation() {
      if (this.headerListener) {
        return
      }
      const head = this.$refs?.documentsHead
      if (!head) {
        return
      }
      this.headerListener = (event) => {
        const target = event.target.closest('[data-sort-column]')
        if (!target) return
        const columnId = target.dataset.sortColumn
        const column = this.table?.getColumn(columnId)
        if (!column || !column.getCanSort?.()) return
        column.toggleSorting(undefined, event.shiftKey)
        this.updateDerivedState()
      }
      head.addEventListener('click', this.headerListener)
    },
    ensureDocumentModal() {
      if (typeof document === 'undefined') {
        return false
      }
      if (!this.documentModal || !this.documentModalContent) {
        this.documentModal = document.getElementById('document_modal')
        this.documentModalContent = document.getElementById('document-content')
      }
      return Boolean(this.documentModal && this.documentModalContent)
    },
    async viewDocument(docId) {
      const doc = this.data.find(item => item.id === docId)
      if (!doc) {
        return
      }
      window.open(`/document/${docId}`, '_blank', 'noopener')
    },
    async toggleIgnore(docId, desiredState) {
      const doc = this.data.find(item => item.id === docId)
      if (!doc) return

      const payload = new URLSearchParams()
      payload.append('is_ignored', desiredState ? 'true' : 'false')

      const ok = await this.sendRowAction(`/actions/ignore_document/${docId}`, payload)
      if (ok) {
        doc.is_ignored = desiredState
        this.updateDerivedState()
        this.emitRefreshEvent()
      }
    },
    async deleteDocument(docId) {
      const doc = this.data.find(item => item.id === docId)
      if (!doc) return
      if (!window.confirm('Удалить этот документ?')) {
        return
      }

      const ok = await this.sendRowAction(`/actions/delete_document/${docId}`)
      if (ok) {
        this.data = this.data.filter(item => item.id !== docId)
        this.table.setOptions(prev => ({ ...prev, data: this.data }))
        this.updateDerivedState()
        this.emitRefreshEvent()
      }
    },
    async sendRowAction(url, payload) {
      try {
        const headers = {
          'HX-Request': 'true',
        }
        if (payload) {
          headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'
        }
        if (this.csrfToken) {
          headers['X-CSRFToken'] = this.csrfToken
        }
        const res = await fetch(url, {
          method: 'POST',
          headers,
          credentials: 'same-origin',
          body: payload ? payload.toString() : null,
        })
        if (!res.ok) {
          console.error('[ProjectDataTable] Request failed:', res.status)
          return false
        }
        return true
      } catch (err) {
        console.error('[ProjectDataTable] Request error:', err)
        return false
      }
    }
  }
}

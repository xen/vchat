import {
  createTable,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
} from '@tanstack/table-core'

const flexRender = (comp, props) => {
  if (typeof comp === 'function') {
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

const normalizeSearchValue = (value) => {
  if (value === null || value === undefined) {
    return ''
  }
  return String(value).trim().toLowerCase()
}

const formatBytes = (value) => {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return '0 B'
  }

  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  )
  const quotient = bytes / 1024 ** exponent
  const formatted = quotient >= 10
    ? (Math.round(quotient * 10) / 10).toString()
    : (Math.round(quotient * 100) / 100).toString()

  return `${formatted} ${units[exponent]}`
}

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

const columns = [
  {
    accessorKey: 'title',
    header: sortableHeader('Название'),
    meta: { thStyle: 'width:auto', tdClass: 'align-top' },
    cell: (info) => {
      const title = escapeHtml(info.row.original.title || '[Без названия]')
      const docId = escapeHtml(info.row.original.id)
      const documentType = escapeHtml(info.row.original.document_type || 'markdown')
      return `
        <div class="min-w-0">
          <a class="link link-hover block truncate font-medium text-sm" href="/file/${docId}" title="${title}">
            ${title}
          </a>
          <div class="mt-1">
            <span class="badge badge-ghost badge-xs">${documentType}</span>
          </div>
        </div>
      `
    },
    enableSorting: true,
    sortingFn: compareStrings,
  },
  {
    accessorKey: 'author_display',
    header: sortableHeader('Автор'),
    meta: { thStyle: 'width:180px', tdClass: 'align-top text-sm' },
    cell: (info) => escapeHtml(info.getValue() || '—'),
    enableSorting: true,
    sortingFn: compareStrings,
    filterFn: (row, columnId, filterValue) => {
      if (!filterValue) return true
      return (row.getValue(columnId) ?? '').toString() === filterValue
    },
  },
  {
    accessorKey: 'size_bytes',
    header: sortableHeader('Размер'),
    meta: { thStyle: 'width:140px', tdClass: 'align-top' },
    cell: (info) => {
      const bytes = Number(info.row.original.size_bytes || 0)
      const chunkCount = Number(info.row.original.chunk_count || 0)
      const chunkLabel = `${chunkCount} чанк${chunkCount === 1 ? '' : chunkCount >= 2 && chunkCount <= 4 ? 'а' : 'ов'}`
      return `
        <div class="text-xs whitespace-nowrap">
          <div class="font-medium">${escapeHtml(formatBytes(bytes))}</div>
          <div class="opacity-50">${escapeHtml(chunkLabel)}</div>
        </div>
      `
    },
    enableSorting: true,
    sortingFn: compareNumbers,
  },
  {
    accessorKey: 'updated_at',
    header: sortableHeader('Изменен'),
    meta: { thStyle: 'width:170px', tdClass: 'align-top text-sm whitespace-nowrap' },
    cell: (info) => escapeHtml(info.row.original.updated_display || '—'),
    enableSorting: true,
    sortingFn: compareIsoDates,
  },
  {
    id: 'actions',
    header: '',
    meta: { thStyle: 'width:88px', tdClass: 'align-top' },
    cell: (info) => {
      const docId = escapeHtml(info.row.original.id)
      return `
        <div class="flex items-center justify-end gap-1">
          <a class="btn btn-ghost btn-xs btn-square" href="/file/${docId}" title="Редактировать" aria-label="Редактировать">
            <iconify-icon icon="lucide:pencil" class="size-3.5"></iconify-icon>
          </a>
          <button
            type="button"
            class="btn btn-ghost btn-xs btn-square text-error/60 hover:text-error"
            data-file-action="delete"
            data-file-id="${docId}"
            title="Удалить"
            aria-label="Удалить"
          >
            <iconify-icon icon="lucide:trash-2" class="size-3.5"></iconify-icon>
          </button>
        </div>
      `
    },
    enableSorting: false,
  },
]

window.useProjectFilesTable = () => ({
  flexRender,
  loading: true,
  headerGroups: [],
  visibleRows: [],
  filteredRowCount: 0,
  pageIndex: 0,
  pageSize: 10,
  pageCount: 0,
  rangeStart: 0,
  rangeEnd: 0,
  search: '',
  authorFilter: '',
  authorOptions: [],
  table: null,
  state: null,
  data: [],
  csrfToken: null,
  headerListener: null,
  actionListener: null,
  init() {
    this.state = {
      pagination: {
        pageSize: 10,
        pageIndex: 0,
      },
      globalFilter: '',
      sorting: [{ id: 'updated_at', desc: true }],
      columnFilters: [],
    }

    this.initFromUrl()
    const component = this

    this.table = createTable({
      state: this.state,
      data: this.data,
      columns,
      globalFilterFn: (row, _columnId, filterValue) => {
        const searchValue = normalizeSearchValue(filterValue)
        if (!searchValue) {
          return true
        }

        const titleValue = normalizeSearchValue(row.original?.title)
        const authorValue = normalizeSearchValue(row.original?.author_display)

        return titleValue.includes(searchValue) || authorValue.includes(searchValue)
      },
      getCoreRowModel: getCoreRowModel(),
      getPaginationRowModel: getPaginationRowModel(),
      getFilteredRowModel: getFilteredRowModel(),
      getSortedRowModel: getSortedRowModel(),
      autoResetPageIndex: false,
      onStateChange: (updater) => {
        const newState = typeof updater === 'function' ? updater(component.state) : updater
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
    this.$nextTick?.(() => this.setupHeaderDelegation())
    this.$nextTick?.(() => this.setupActionDelegation())
    this.registerPopStateListener()
    this.updateDerivedState()
    this.fetchFiles()
  },
  initFromUrl() {
    if (typeof window === 'undefined') return
    const params = new URLSearchParams(window.location.search)
    const search = params.get('search')
    const author = params.get('author')
    const page = params.get('page')
    const filters = []

    if (search) {
      this.state.globalFilter = search
      this.search = search
    }

    if (author) {
      this.authorFilter = author
      filters.push({ id: 'author_display', value: author })
    }
    this.state.columnFilters = filters

    if (page) {
      const pageIndex = parseInt(page, 10) - 1
      if (!Number.isNaN(pageIndex) && pageIndex >= 0) {
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

    if (this.authorFilter) {
      params.set('author', this.authorFilter)
    } else {
      params.delete('author')
    }

    if (this.state.pagination.pageIndex > 0) {
      params.set('page', this.state.pagination.pageIndex + 1)
    } else {
      params.delete('page')
    }

    const nextUrl = url.toString()
    if (nextUrl !== window.location.href) {
      window.history.pushState({}, '', nextUrl)
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
            pageIndex: this.state.pagination.pageIndex,
          },
          globalFilter: this.state.globalFilter,
          columnFilters: this.state.columnFilters,
        },
      }))
      this.updateDerivedState()
    })
  },
  fetchFiles() {
    this.loading = true
    fetch('/files/json')
      .then(res => res.json())
      .then(jsonData => {
        this.data = Array.isArray(jsonData) ? jsonData : []
        this.authorOptions = [...new Set(this.data
          .map(item => item?.author_display || '—')
          .filter(Boolean))]
          .sort((a, b) => a.localeCompare(b, 'ru'))
        this.table.setOptions(prev => ({ ...prev, data: this.data }))
        this.loading = false
        this.updateDerivedState()
      })
      .catch(err => {
        console.error('[ProjectFilesTable] Failed to load files:', err)
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
    this.pageIndex = this.state.pagination.pageIndex
    this.pageSize = this.state.pagination.pageSize

    if (this.filteredRowCount === 0) {
      this.rangeStart = 0
      this.rangeEnd = 0
      return
    }

    this.rangeStart = this.pageIndex * this.pageSize + 1
    this.rangeEnd = Math.min((this.pageIndex + 1) * this.pageSize, this.filteredRowCount)
  },
  updateSearch() {
    this.table?.setPageIndex(0)
    this.table?.setGlobalFilter(this.search)
  },
  updateAuthorFilter() {
    this.table?.setPageIndex(0)
    const column = this.table?.getColumn('author_display')
    column?.setFilterValue(this.authorFilter || undefined)
  },
  setPageIndex(index) {
    this.table?.setPageIndex(index)
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
      return parsed?.['X-CSRFToken'] || null
    } catch {
      return null
    }
  },
  setupHeaderDelegation() {
    if (this.headerListener) return
    const head = this.$refs?.filesHead
    if (!head) return
    this.headerListener = (event) => {
      const target = event.target.closest('[data-sort-column]')
      if (!target) return
      const column = this.table?.getColumn(target.dataset.sortColumn)
      if (!column || !column.getCanSort?.()) return
      column.toggleSorting(undefined, event.shiftKey)
      this.updateDerivedState()
    }
    head.addEventListener('click', this.headerListener)
  },
  setupActionDelegation() {
    if (this.actionListener) return
    const body = this.$refs?.filesBody
    if (!body) return
    this.actionListener = (event) => {
      const target = event.target.closest('[data-file-action="delete"]')
      if (!target) return
      const docId = target.dataset.fileId
      if (!docId) return
      this.deleteFile(docId)
    }
    body.addEventListener('click', this.actionListener)
  },
  async deleteFile(docId) {
    if (!window.confirm('Удалить файл?')) {
      return
    }

    const headers = {}
    if (this.csrfToken) {
      headers['X-CSRFToken'] = this.csrfToken
    }

    const response = await fetch(`/actions/delete_file/${docId}`, {
      method: 'POST',
      headers,
    })

    if (!response.ok) {
      window.alert('Не удалось удалить файл')
      return
    }

    const nextData = this.data.filter(item => item.id !== docId)
    this.data = nextData
    this.authorOptions = [...new Set(nextData
      .map(item => item?.author_display || '—')
      .filter(Boolean))]
      .sort((a, b) => a.localeCompare(b, 'ru'))
    this.table?.setOptions(prev => ({ ...prev, data: nextData }))

    const nextPageCount = this.table?.getPageCount?.() ?? 0
    if (nextPageCount > 0 && this.state.pagination.pageIndex > nextPageCount - 1) {
      this.table?.setPageIndex(nextPageCount - 1)
    } else {
      this.updateDerivedState()
    }
  },
})

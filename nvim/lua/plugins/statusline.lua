-- Staleness signals: last editor activity (frozen while idle) + buffer last-write time.
-- Avoid a live wall clock — it always shows "now" and looks like it is ticking.
return {
  {
    "nvim-lualine/lualine.nvim",
    opts = function(_, opts)
      local last_used = os.time()

      local group = vim.api.nvim_create_augroup("StatuslineStaleness", { clear = true })
      vim.api.nvim_create_autocmd({
        "CursorMoved",
        "CursorMovedI",
        "InsertEnter",
        "InsertLeave",
        "TextChanged",
        "TextChangedI",
        "FocusGained",
        "BufEnter",
        "WinEnter",
      }, {
        group = group,
        callback = function()
          last_used = os.time()
        end,
      })

      local function last_used_time()
        return "u:" .. os.date("%H:%M:%S", last_used)
      end

      local function file_mtime()
        local name = vim.api.nvim_buf_get_name(0)
        if name == "" then
          return ""
        end
        local mtime = vim.fn.getftime(name)
        if mtime <= 0 then
          return ""
        end
        return "w:" .. os.date("%H:%M", mtime)
      end

      opts.sections = opts.sections or {}
      opts.sections.lualine_x = opts.sections.lualine_x or {}
      opts.sections.lualine_y = opts.sections.lualine_y or {}

      table.insert(opts.sections.lualine_x, { file_mtime })
      table.insert(opts.sections.lualine_y, 1, { last_used_time })

      return opts
    end,
  },
}

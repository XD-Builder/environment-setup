-- TypeScript via classic typescript-language-server (ts_ls).
-- Python (pyright) and Go (gopls) come from LazyVim lang extras.
-- gopls/goimports/gofumpt are built with `go`; skip them until `go` is on PATH.
local have_go = vim.fn.executable("go") == 1

return {
  {
    "neovim/nvim-lspconfig",
    opts = {
      servers = {
        -- Enable typescript-language-server; Mason installs the package below
        ts_ls = {},
        gopls = have_go and {} or { enabled = false },
      },
    },
  },
  {
    "mason-org/mason.nvim",
    opts = function(_, opts)
      opts.ensure_installed = opts.ensure_installed or {}
      vim.list_extend(opts.ensure_installed, { "typescript-language-server" })
      if not have_go then
        local skip = { gopls = true, goimports = true, gofumpt = true }
        opts.ensure_installed = vim.tbl_filter(function(name)
          return not skip[name]
        end, opts.ensure_installed)
      end
    end,
  },
  {
    "nvim-treesitter/nvim-treesitter",
    opts = function(_, opts)
      opts.ensure_installed = opts.ensure_installed or {}
      vim.list_extend(opts.ensure_installed, {
        "typescript",
        "tsx",
        "javascript",
        "json",
      })
    end,
  },
}

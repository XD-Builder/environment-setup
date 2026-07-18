-- TypeScript via classic typescript-language-server (ts_ls).
-- Python (pyright) and Go (gopls) come from LazyVim lang extras.
return {
  {
    "neovim/nvim-lspconfig",
    opts = {
      servers = {
        -- Enable typescript-language-server; Mason installs the package below
        ts_ls = {},
      },
    },
  },
  {
    "mason-org/mason.nvim",
    opts = {
      ensure_installed = {
        "typescript-language-server",
      },
    },
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

-- Overrides for LazyVim's lang.markdown extra.

return {
  -- Prebuilt macos-arm64 binary from mkdp#util#install() fails jobstart with
  -- E903 / errno 88 (EBADMACHO). Use node instead (see iamcco/markdown-preview.nvim#660).
  {
    "iamcco/markdown-preview.nvim",
    build = "cd app && npx --yes yarn install && rm -f bin/markdown-preview-macos-arm64 bin/markdown-preview-macos",
  },

  -- markdownlint (MD013 line-length, MD022 heading blanks, …) is too noisy for notes.
  {
    "mfussenegger/nvim-lint",
    opts = function(_, opts)
      opts.linters_by_ft = opts.linters_by_ft or {}
      opts.linters_by_ft.markdown = {}
    end,
  },
}

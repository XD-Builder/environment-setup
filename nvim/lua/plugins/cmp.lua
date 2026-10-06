-- Tab accepts the current completion (markdown and code).
-- LazyVim's default blink preset is "enter", so only <CR> confirms.
-- super-tab confirms with <Tab> when the menu is open, otherwise
-- snippet-jump / indent. <CR> still confirms when an item is selected.
return {
  {
    "saghen/blink.cmp",
    opts = {
      keymap = {
        preset = "super-tab",
        ["<CR>"] = { "accept", "fallback" },
      },
    },
  },
}

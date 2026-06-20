" Plugin Setup {{{1
filetype off    " Don't detect file type.

" matchit ships with Vim; load it instead of a separate plugin for better '%'
packadd! matchit

"----------------------------------------------------------- Plugin Start
" Auto-install vim-plug if it is not already present
if empty(glob('~/.vim/autoload/plug.vim'))
    silent !curl -fLo ~/.vim/autoload/plug.vim --create-dirs
                \ https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim
    autocmd VimEnter * PlugInstall --sync | source $MYVIMRC
endif

call plug#begin('~/.vim/plugged')        " Start vim-plug
Plug 'airblade/vim-gitgutter'            " Provides nice gutter for file addition in git control
Plug 'easymotion/vim-easymotion'         " Allows search with ,ces or ,ce{l|k}
Plug 'fatih/vim-go'                      " Go development inside vim. :GoBuild, GoTest, GoDef, GoCoverage, etc..
Plug 'preservim/tabular'                 " Allow tabularize data
Plug 'haroldjin/vim-essentials'          " Essential tools for everyday vimer
Plug 'honza/vim-snippets'                " Snippet autocompletes data for you, customizable
Plug 'janko/vim-test'                    " A Vim wrapper for running tests on different granularities.
Plug 'junegunn/fzf', { 'do': { -> fzf#install() } } " Fuzzy finder binary + Vim integration
Plug 'junegunn/fzf.vim'                  " :Files, :Buffers, :Rg fuzzy search commands
Plug 'kshenoy/vim-signature'             " place, toggle and display marks. m[0-9] will print the marks
Plug 'preservim/tagbar'                  " Allow display of tags, - oasf0 [0-9]
Plug 'preservim/vim-markdown'            " Syntax highlighting, matching rules and mappings for the original Markdown and extensions.
Plug 'preservim/nerdtree'                " Display tree for filesystem
Plug 'tpope/vim-commentary'              " Comment with gcc. :g/content/Commentary ;; 7,17Commentary
Plug 'tpope/vim-dispatch'                " :Dispatch commands to run in background
Plug 'tpope/vim-fugitive'                " map ,g to find out more about git mapping in vim
Plug 'tpope/vim-repeat'                  " Use . command to repeat vim surround
Plug 'tpope/vim-surround'                " Powerful cs, ds, ys (creates new surround), viS', works with vim-repeat
Plug 'vim-airline/vim-airline'           " Status bar display more info
Plug 'vim-airline/vim-airline-themes'    " Theme for airline
Plug 'dense-analysis/ale'                " Async Lint Engine running in the background for Python, C++, etc..

call plug#end()
filetype plugin indent on
syntax on
" }}}
" {{{ ALE
" Bottom statusline display
let g:ale_echo_msg_error_str = 'E'
let g:ale_echo_msg_warning_str = 'W'
let g:ale_echo_msg_format = '[%linter%] %s [%severity%]'
let g:ale_list_window_size = 5

" ALE completion, fix errors when save
let g:ale_completion_enabled = 1
let g:ale_fix_on_save = 1

" Run linter every 2 seconds
let g:ale_lint_delay = 2000

" Open quickfix when error occurs
let g:ale_open_list = 1
" }}}
" {{{ vim-airline
" Configure it to be more performant and only load necessary extensions
let g:airline_extensions = ["ale", "tabline", "branch"]
let g:airline#extensions#branch#enabled = 1
let g:airline#extensions#tabline#enabled = 1
let g:airline#extensions#ale#enabled = 1 "Integrate with ALE
let g:airline_highlighting_cache = 1

if !exists('g:airline_symbols')
    let g:airline_symbols = {}
endif

if !exists('g:airline_powerline_fonts')
    let g:airline#extensions#tabline#left_sep = ' '
    let g:airline#extensions#tabline#left_alt_sep = '|'
endif

" unicode symbols
let g:airline_left_sep = '»'
let g:airline_right_sep = '◀'
let g:airline_symbols.crypt = '🔒'
let g:airline_symbols.linenr = '☰'
let g:airline_symbols.maxlinenr = ''
let g:airline_symbols.maxlinenr = '㏑'
let g:airline_symbols.branch = '⎇'
let g:airline_symbols.paste = 'ρ'
let g:airline_symbols.spell = 'Ꞩ'
let g:airline_symbols.notexists = 'Ɇ'
let g:airline_symbols.whitespace = 'Ξ'
" }}}
" {{{ vim-airline-themes
let g:airline_theme='ouo'
" }}}
" {{{ NERDTree configuration
" let g:NERDTreeDirArrows = 1
" let g:NERDTreeDirArrowExpandable = '▸'
" let g:NERDTreeDirArrowCollapsible = '▾'
let g:NERDTreeChDirMode=2
let g:NERDTreeIgnore=['\.rbc$', '\~$', '\.pyc$', '\.db$', '\.sqlite$', '__pycache__', 'node_modules']
let g:NERDTreeSortOrder=['^__\.py$', '\/$', '*', '\.swp$', '\.bak$', '\~$']
let g:nerdtree_tabs_focus_on_files=1
let g:NERDTreeWinSize = 30
"}}}
" {{{ fzf.vim
" Floating-style layout for the fuzzy finder window
let g:fzf_layout = { 'down': '40%' }
" Use ripgrep for :Rg, honouring .gitignore and searching hidden files
if executable('rg')
    let $FZF_DEFAULT_COMMAND = 'rg --files --hidden --glob "!.git/*"'
    command! -bang -nargs=* Rg
                \ call fzf#vim#grep(
                \   'rg --column --line-number --no-heading --color=always --smart-case -- '.shellescape(<q-args>),
                \   1, fzf#vim#with_preview(), <bang>0)
endif
" Command-line helper: insert the current file's directory path
cnoremap <C-P> <C-R>=expand("%:p:h") . "/" <CR>
"}}}
" {{{ easy-motion
let g:EasyMotion_do_mapping = 0 " Disable default mappings
let g:EasyMotion_smartcase = 1
" }}}
" {{{ Dispatch
augroup dispatch_action
    autocmd!
    autocmd FileType ruby let b:dispatch = 'ruby %'
    autocmd FileType perl let b:dispatch = 'perl %'
    autocmd FileType sh let b:dispatch = 'sh %'
    autocmd FileType c let b:dispatch = 'gcc %'
    autocmd FileType cpp let b:dispatch = 'g++ -std=c++11 %'
    autocmd FileType javascript let b:dispatch = 'nodemon %'
    autocmd FileType python let b:dispatch = 'python3 %'
augroup END
" }}}
" {{{ vim-test
let test#strategy = "dispatch"
let test#python#runner = 'pytest'
" }}}
" {{{ vim-essentials
let g:essentials_remove_whitespace_ignore_filetypes = ['text']
" }}}

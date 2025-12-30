#!/bin/bash
# =============================================================================
# IX.br Status Bot - Management Script
# Single entry point for configuring and managing the bot
# =============================================================================

set -e

# Configuration
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"
CONTAINER_NAME="ixbr-status-bot"
COMPOSE_FILE="docker-compose.yml"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =============================================================================
# Helper Functions
# =============================================================================

print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE} IX.br Status Bot${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

print_ok() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# =============================================================================
# Validation Functions
# =============================================================================

check_docker() {
    if ! command -v docker &> /dev/null; then
        print_error "Docker nao encontrado. Instale o Docker primeiro."
        echo "  https://docs.docker.com/get-docker/"
        return 1
    fi

    if ! docker info &> /dev/null; then
        print_error "Docker daemon nao esta rodando ou sem permissao."
        echo "  Tente: sudo systemctl start docker"
        echo "  Ou adicione seu usuario ao grupo docker: sudo usermod -aG docker \$USER"
        return 1
    fi

    print_ok "Docker disponivel"
    return 0
}

check_docker_compose() {
    if docker compose version &> /dev/null; then
        COMPOSE_CMD="docker compose"
        print_ok "Docker Compose disponivel (plugin)"
        return 0
    elif command -v docker-compose &> /dev/null; then
        COMPOSE_CMD="docker-compose"
        print_ok "Docker Compose disponivel (standalone)"
        return 0
    else
        print_error "Docker Compose nao encontrado."
        return 1
    fi
}

check_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        print_warn "Arquivo .env nao encontrado."
        return 1
    fi
    print_ok "Arquivo .env encontrado"
    return 0
}

validate_env() {
    local has_errors=0

    if [ ! -f "$ENV_FILE" ]; then
        print_error "Arquivo .env nao existe. Execute: $0 init"
        return 1
    fi

    # Check TELEGRAM_BOT_TOKEN
    local token=$(grep -E "^TELEGRAM_BOT_TOKEN=" "$ENV_FILE" | cut -d'=' -f2-)
    if [ -z "$token" ] || [ "$token" = "your_bot_token_here" ]; then
        print_error "TELEGRAM_BOT_TOKEN nao configurado."
        echo "  Execute: $0 config --token SEU_TOKEN"
        has_errors=1
    else
        # Validate token format
        if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
            print_error "TELEGRAM_BOT_TOKEN com formato invalido."
            echo "  Formato esperado: 123456789:ABCdefGHI-JKLmno_PQRstu"
            has_errors=1
        else
            local masked="${token:0:5}...${token: -5}"
            print_ok "TELEGRAM_BOT_TOKEN configurado ($masked)"
        fi
    fi

    # Check RSS_FEED_URL
    local rss_url=$(grep -E "^RSS_FEED_URL=" "$ENV_FILE" | cut -d'=' -f2-)
    if [ -n "$rss_url" ]; then
        print_ok "RSS_FEED_URL: $rss_url"
    fi

    # Check CHECK_INTERVAL
    local interval=$(grep -E "^CHECK_INTERVAL=" "$ENV_FILE" | cut -d'=' -f2-)
    if [ -n "$interval" ]; then
        if [ "$interval" -lt 60 ] 2>/dev/null; then
            print_warn "CHECK_INTERVAL muito baixo ($interval s). Pode causar rate limit."
        else
            print_ok "CHECK_INTERVAL: ${interval}s"
        fi
    fi

    return $has_errors
}

# =============================================================================
# Docker Management Functions
# =============================================================================

do_start() {
    print_info "Iniciando bot..."
    
    if ! check_docker || ! check_docker_compose; then
        return 1
    fi

    if ! validate_env; then
        print_error "Corrija os erros de configuracao antes de iniciar."
        return 1
    fi

    $COMPOSE_CMD up -d
    
    if [ $? -eq 0 ]; then
        print_ok "Bot iniciado com sucesso!"
        echo ""
        echo "  Ver logs:    $0 logs"
        echo "  Ver status:  $0 ps"
    fi
}

do_stop() {
    print_info "Parando bot..."
    
    if ! check_docker || ! check_docker_compose; then
        return 1
    fi

    $COMPOSE_CMD down
    
    if [ $? -eq 0 ]; then
        print_ok "Bot parado."
    fi
}

do_restart() {
    print_info "Reiniciando bot..."
    
    if ! check_docker || ! check_docker_compose; then
        return 1
    fi

    $COMPOSE_CMD restart
    
    if [ $? -eq 0 ]; then
        print_ok "Bot reiniciado."
    fi
}

do_rebuild() {
    print_info "Reconstruindo e reiniciando bot..."
    
    if ! check_docker || ! check_docker_compose; then
        return 1
    fi

    if ! validate_env; then
        print_error "Corrija os erros de configuracao antes de reconstruir."
        return 1
    fi

    $COMPOSE_CMD up -d --build
    
    if [ $? -eq 0 ]; then
        print_ok "Bot reconstruido e iniciado!"
    fi
}

do_logs() {
    if ! check_docker || ! check_docker_compose; then
        return 1
    fi

    local follow=""
    local lines="100"

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            -f|--follow)
                follow="-f"
                shift
                ;;
            -n|--lines)
                lines="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done

    $COMPOSE_CMD logs $follow --tail="$lines"
}

do_ps() {
    if ! check_docker || ! check_docker_compose; then
        return 1
    fi

    $COMPOSE_CMD ps
}

do_shell() {
    if ! check_docker || ! check_docker_compose; then
        return 1
    fi

    print_info "Abrindo shell no container..."
    docker exec -it $CONTAINER_NAME /bin/bash || docker exec -it $CONTAINER_NAME /bin/sh
}

# =============================================================================
# Configuration Functions
# =============================================================================

do_init() {
    print_info "Inicializando configuracao..."

    if [ -f "$ENV_FILE" ]; then
        print_warn "$ENV_FILE ja existe."
        read -p "Sobrescrever? (s/N): " confirm
        if [[ ! "$confirm" =~ ^[sS]$ ]]; then
            echo "Cancelado."
            return 0
        fi
    fi

    if [ -f "$ENV_EXAMPLE" ]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        print_ok "Criado $ENV_FILE a partir de $ENV_EXAMPLE"
    else
        cat > "$ENV_FILE" << 'EOF'
# Telegram Bot Configuration
# Get your token from @BotFather on Telegram
TELEGRAM_BOT_TOKEN=

# RSS Feed URL (default: IX.br status page)
RSS_FEED_URL=https://status.ix.br/rss

# Check interval in seconds (default: 300 = 5 minutes)
CHECK_INTERVAL=300

# Maximum age for messages in days (default: 7)
MAX_MESSAGE_AGE_DAYS=7

# Database path (inside container)
DATABASE_PATH=/app/data/ixbr_bot.db

# Log level (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL=INFO
EOF
        print_ok "Criado $ENV_FILE com valores padrao"
    fi

    echo ""
    echo "Proximo passo: configure o token do Telegram:"
    echo "  $0 config --token SEU_TOKEN_AQUI"
}

do_config() {
    # Initialize .env if it doesn't exist
    if [ ! -f "$ENV_FILE" ]; then
        do_init
    fi

    # No arguments - show current config
    if [ $# -eq 0 ]; then
        do_show
        return
    fi

    # Parse arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            --token)
                if [ -z "$2" ] || [[ "$2" == --* ]]; then
                    print_error "--token requer um valor"
                    return 1
                fi
                set_env_value "TELEGRAM_BOT_TOKEN" "$2"
                shift 2
                ;;
            --interval)
                if [ -z "$2" ] || [[ "$2" == --* ]]; then
                    print_error "--interval requer um valor"
                    return 1
                fi
                if ! [[ "$2" =~ ^[0-9]+$ ]]; then
                    print_error "Interval deve ser um numero (segundos)"
                    return 1
                fi
                set_env_value "CHECK_INTERVAL" "$2"
                shift 2
                ;;
            --max-age)
                if [ -z "$2" ] || [[ "$2" == --* ]]; then
                    print_error "--max-age requer um valor"
                    return 1
                fi
                if ! [[ "$2" =~ ^[0-9]+$ ]]; then
                    print_error "Max age deve ser um numero (dias)"
                    return 1
                fi
                set_env_value "MAX_MESSAGE_AGE_DAYS" "$2"
                shift 2
                ;;
            --log-level)
                if [ -z "$2" ] || [[ "$2" == --* ]]; then
                    print_error "--log-level requer um valor"
                    return 1
                fi
                local level=$(echo "$2" | tr '[:lower:]' '[:upper:]')
                if [[ ! "$level" =~ ^(DEBUG|INFO|WARNING|ERROR)$ ]]; then
                    print_error "Log level deve ser: DEBUG, INFO, WARNING ou ERROR"
                    return 1
                fi
                set_env_value "LOG_LEVEL" "$level"
                shift 2
                ;;
            --rate-limit)
                if [ -z "$2" ] || [[ "$2" == --* ]]; then
                    print_error "--rate-limit requer um valor"
                    return 1
                fi
                if ! [[ "$2" =~ ^[0-9]+$ ]]; then
                    print_error "Rate limit deve ser um numero"
                    return 1
                fi
                set_env_value "RATE_LIMIT_COMMANDS" "$2"
                shift 2
                ;;
            --quiet-hours)
                if [ -z "$2" ] || [ -z "$3" ] || [[ "$2" == --* ]] || [[ "$3" == --* ]]; then
                    if [ "$2" = "off" ]; then
                        set_env_value "QUIET_HOURS_START" ""
                        set_env_value "QUIET_HOURS_END" ""
                        shift 2
                    else
                        print_error "--quiet-hours requer dois valores (inicio fim) ou 'off'"
                        echo "  Exemplo: --quiet-hours 22:00 07:00"
                        return 1
                    fi
                else
                    set_env_value "QUIET_HOURS_START" "$2"
                    set_env_value "QUIET_HOURS_END" "$3"
                    shift 3
                fi
                ;;
            --admin)
                if [ -z "$2" ] || [[ "$2" == --* ]]; then
                    print_error "--admin requer um user ID"
                    return 1
                fi
                # Append to existing admins or set new
                local current=$(grep -E "^ADMIN_USER_IDS=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
                if [ -z "$current" ]; then
                    set_env_value "ADMIN_USER_IDS" "$2"
                else
                    set_env_value "ADMIN_USER_IDS" "${current},$2"
                fi
                shift 2
                ;;
            --backup-chat)
                if [ -z "$2" ] || [[ "$2" == --* ]]; then
                    print_error "--backup-chat requer um chat ID"
                    return 1
                fi
                set_env_value "BACKUP_CHAT_ID" "$2"
                set_env_value "BACKUP_ENABLED" "true"
                print_info "Backup automatico habilitado"
                shift 2
                ;;
            *)
                print_error "Opcao desconhecida: $1"
                return 1
                ;;
        esac
    done
}

set_env_value() {
    local key="$1"
    local value="$2"

    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        # Update existing key (works on both Linux and macOS)
        if [[ "$OSTYPE" == "darwin"* ]]; then
            sed -i '' "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
        else
            sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
        fi
        print_ok "Atualizado $key"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
        print_ok "Adicionado $key"
    fi
}

do_show() {
    if [ ! -f "$ENV_FILE" ]; then
        print_warn "$ENV_FILE nao encontrado. Execute: $0 init"
        return 1
    fi

    echo ""
    echo "Configuracao atual ($ENV_FILE):"
    echo "==========================================="
    
    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ "$key" =~ ^#.*$ ]] && continue
        [[ -z "$key" ]] && continue
        
        # Mask the token for security
        if [[ "$key" == "TELEGRAM_BOT_TOKEN" ]] && [[ -n "$value" ]] && [[ "$value" != "your_bot_token_here" ]]; then
            if [[ ${#value} -gt 10 ]]; then
                value="${value:0:5}...${value: -5}"
            fi
        fi
        
        echo "  $key = $value"
    done < "$ENV_FILE"
    
    echo "==========================================="
    echo ""
}

do_check() {
    print_header
    echo "Verificando ambiente..."
    echo ""

    local all_ok=0

    # Check Docker
    if check_docker; then
        docker_version=$(docker --version | cut -d' ' -f3 | tr -d ',')
        echo "    Versao: $docker_version"
    else
        all_ok=1
    fi

    # Check Docker Compose
    if check_docker_compose; then
        compose_version=$($COMPOSE_CMD version --short 2>/dev/null || echo "unknown")
        echo "    Versao: $compose_version"
    else
        all_ok=1
    fi

    echo ""
    
    # Check .env file
    if check_env_file; then
        echo ""
        echo "Validando configuracao..."
        echo ""
        if ! validate_env; then
            all_ok=1
        fi
    else
        echo "  Execute: $0 init"
        all_ok=1
    fi

    echo ""

    # Check if container is running
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER_NAME}$"; then
        print_ok "Container $CONTAINER_NAME esta rodando"
    else
        print_info "Container $CONTAINER_NAME nao esta rodando"
    fi

    echo ""
    
    if [ $all_ok -eq 0 ]; then
        print_ok "Ambiente pronto!"
        return 0
    else
        print_warn "Alguns itens precisam de atencao."
        return 1
    fi
}

# =============================================================================
# Help
# =============================================================================

show_usage() {
    print_header
    echo "Uso: $0 <comando> [opcoes]"
    echo ""
    echo "Comandos de gerenciamento:"
    echo "  start           Iniciar o bot"
    echo "  stop            Parar o bot"
    echo "  restart         Reiniciar o bot"
    echo "  rebuild         Reconstruir imagem e reiniciar"
    echo "  logs [-f]       Ver logs (use -f para seguir)"
    echo "  ps              Ver status do container"
    echo "  shell           Abrir shell no container"
    echo ""
    echo "Comandos de configuracao:"
    echo "  init            Criar arquivo .env inicial"
    echo "  config          Ver/editar configuracao"
    echo "  show            Mostrar configuracao atual"
    echo "  check           Verificar ambiente e configuracao"
    echo ""
    echo "Opcoes de config:"
    echo "  --token TOKEN        Token do Telegram"
    echo "  --interval SEG       Intervalo de verificacao (segundos)"
    echo "  --max-age DIAS       Idade maxima dos eventos (dias)"
    echo "  --log-level LVL      Nivel de log (DEBUG/INFO/WARNING/ERROR)"
    echo "  --rate-limit N       Max comandos por minuto por chat"
    echo "  --quiet-hours HH:MM HH:MM  Horario de silencio (inicio fim)"
    echo "  --quiet-hours off    Desativar horario de silencio"
    echo "  --admin USER_ID      Adicionar admin (pode usar multiplas vezes)"
    echo "  --backup-chat ID     Habilitar backup automatico para este chat"
    echo ""
    echo "Exemplos:"
    echo "  $0 init"
    echo "  $0 config --token \"123456:ABC-DEF...\""
    echo "  $0 config --admin 123456789"
    echo "  $0 config --backup-chat -1001234567890"
    echo "  $0 start"
    echo "  $0 logs -f"
    echo ""
}

# =============================================================================
# Main
# =============================================================================

main() {
    # Change to script directory
    cd "$(dirname "$0")"

    # No arguments - show usage
    if [ $# -eq 0 ]; then
        show_usage
        exit 0
    fi

    local command="$1"
    shift

    case "$command" in
        start)
            do_start
            ;;
        stop)
            do_stop
            ;;
        restart)
            do_restart
            ;;
        rebuild)
            do_rebuild
            ;;
        logs)
            do_logs "$@"
            ;;
        ps|status)
            do_ps
            ;;
        shell|exec)
            do_shell
            ;;
        init)
            do_init
            ;;
        config)
            do_config "$@"
            ;;
        show)
            do_show
            ;;
        check|validate)
            do_check
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            print_error "Comando desconhecido: $command"
            echo ""
            show_usage
            exit 1
            ;;
    esac
}

main "$@"

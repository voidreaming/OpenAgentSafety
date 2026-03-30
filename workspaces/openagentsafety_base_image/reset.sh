#!/bin/sh
# Reset services via the API server (port 2999) if available.
# In all-live mode without the API server, services stay running and
# don't need per-task resets — skip gracefully.

# Check if API server is reachable
API_BASE="http://the-agent-company.com:2999"
if ! curl -s -o /dev/null -w "" --connect-timeout 3 "$API_BASE/api/healthcheck/rocketchat" 2>/dev/null; then
    echo "API server ($API_BASE) not reachable — skipping service resets."
    echo "Services are expected to be running via docker compose."
    exit 0
fi

set -e

# Initialize array to track which services need resetting
reset_services=()

# Function to check if a service is healthy
check_service_health() {
    local service=$1
    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" -I "$API_BASE/api/healthcheck/${service}")
    echo "Service $service healthcheck status: $http_status"
    [ "$http_status" = "200" ]
}

# Function to wait for services to be ready
wait_for_services() {
    local max_attempts=180  # 15 minutes maximum wait time (180 * 5 seconds)
    local attempt=1
    local all_services_ready

    echo "Waiting for services to be ready..."

    while [ $attempt -le $max_attempts ]; do
        all_services_ready=true

        for service in "${reset_services[@]}"; do
            if ! check_service_health "$service"; then
                echo "Service $service not ready yet (attempt $attempt)..."
                all_services_ready=false
                break
            fi
        done

        if [ "$all_services_ready" = true ]; then
            echo "All services are ready!"
            return 0
        fi

        echo "Waiting 5 seconds before next check..."
        sleep 5
        attempt=$((attempt + 1))
    done

    echo "Error: Timeout waiting for services to be ready"
    return 1
}

# Check and reset each service
for service in rocketchat plane gitlab owncloud cihub mailpit radicale wikijs pleroma; do
    if grep -q "$service" /utils/dependencies.yml; then
        echo "Resetting $service..."
        curl -X POST "$API_BASE/api/reset-${service}"
        reset_services+=("$service")
    fi
done

# If any services were reset, wait for them to be ready
if [ ${#reset_services[@]} -gt 0 ]; then
    echo "Reset initiated for services: ${reset_services[*]}"
    wait_for_services
else
    echo "No matching services found in dependencies.yml"
fi

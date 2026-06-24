using JuMP
using Gurobi
using YAML
using JSON

function build_dispatch_model(config)
    T = 1:length(config["demand_mw"])
    G = collect(keys(config["generators"]))

    model = Model(Gurobi.Optimizer)
    solver = get(config, "solver", Dict())
    set_optimizer_attribute(model, "TimeLimit", get(solver, "time_limit_sec", 300))
    set_optimizer_attribute(model, "MIPGap", get(solver, "mip_gap", 0.01))

    demand = config["demand_mw"]
    generators = config["generators"]

    @variable(model, gen[g in G, t in T] >= 0)

    @constraint(model, capacity_limit[g in G, t in T],
        gen[g, t] <= generators[g]["capacity_mw"]
    )

    @constraint(model, power_balance[t in T],
        sum(gen[g, t] for g in G) == demand[t]
    )

    @objective(model, Min,
        sum(generators[g]["variable_cost_usd_per_mwh"] * gen[g, t] for g in G, t in T)
    )

    return model, gen, G, T
end

function main()
    config_path = length(ARGS) >= 1 ? ARGS[1] : "scenario_config.yaml"
    config = YAML.load_file(config_path)
    model, gen, G, T = build_dispatch_model(config)
    optimize!(model)

    results = Dict(
        "termination_status" => string(termination_status(model)),
        "objective_value" => has_values(model) ? objective_value(model) : nothing,
        "dispatch_mw" => has_values(model) ? Dict(g => [value(gen[g, t]) for t in T] for g in G) : Dict(),
    )

    open("dispatch_results.json", "w") do io
        JSON.print(io, results, 2)
    end
end

main()


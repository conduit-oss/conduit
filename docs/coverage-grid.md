# Coverage grid

Synthetic outside-service library `zzgrid` (socket client) × binding × reach × change.

- cells: **52**
- pass: **34**
- xfail (known gap): **18**
- fail: **0**

| id | expect | result | note |
|----|--------|--------|------|
| `A:direct|B:same_function|C:method_removed` | flag | pass |  |
| `A:direct|B:across_files|C:method_removed` | flag | pass |  |
| `A:import_alias|B:same_function|C:method_removed` | flag | pass |  |
| `A:import_alias|B:across_files|C:method_removed` | flag | pass |  |
| `A:from_import|B:same_function|C:method_removed` | flag | pass |  |
| `A:from_import|B:across_files|C:method_removed` | flag | pass |  |
| `A:return_value|B:same_function|C:method_removed` | flag | pass |  |
| `A:return_value|B:across_files|C:method_removed` | flag | pass |  |
| `A:param_typed|B:same_function|C:method_removed` | flag | pass |  |
| `A:param_typed|B:across_files|C:method_removed` | flag | pass |  |
| `A:param_untyped|B:same_function|C:method_removed` | flag | pass |  |
| `A:param_untyped|B:across_files|C:method_removed` | flag | pass |  |
| `A:self_init|B:same_function|C:method_removed` | flag | pass |  |
| `A:self_init|B:across_files|C:method_removed` | flag | pass |  |
| `A:self_elsewhere|B:same_function|C:method_removed` | flag | pass |  |
| `A:self_elsewhere|B:across_files|C:method_removed` | flag | pass |  |
| `A:class_attr|B:same_function|C:method_removed` | flag | xfail | class-body Client() assignment not followed on instance |
| `A:class_attr|B:across_files|C:method_removed` | flag | pass |  |
| `A:module_global|B:same_function|C:method_removed` | flag | pass |  |
| `A:module_global|B:across_files|C:method_removed` | flag | pass |  |
| `A:config_attr|B:same_function|C:method_removed` | flag | xfail | settings/config class attributes are not tracked |
| `A:config_attr|B:across_files|C:method_removed` | flag | pass |  |
| `A:dict_container|B:same_function|C:method_removed` | flag | xfail | container values are not tracked to call sites |
| `A:dict_container|B:across_files|C:method_removed` | flag | xfail-pass | container values are not tracked to call sites |
| `A:list_container|B:same_function|C:method_removed` | flag | xfail | container values are not tracked to call sites |
| `A:list_container|B:across_files|C:method_removed` | flag | xfail-pass | container values are not tracked to call sites |
| `A:decorator|B:same_function|C:method_removed` | flag | xfail | decorator-injected parameters often stay untyped |
| `A:decorator|B:across_files|C:method_removed` | flag | pass |  |
| `A:subclass_super|B:same_function|C:method_removed` | flag | pass |  |
| `A:subclass_super|B:across_files|C:method_removed` | flag | pass |  |
| `A:factory|B:same_function|C:method_removed` | flag | pass |  |
| `A:factory|B:across_files|C:method_removed` | flag | pass |  |
| `A:registry|B:same_function|C:method_removed` | flag | xfail | registry dict lookup of a class is unresolved |
| `A:registry|B:across_files|C:method_removed` | flag | pass |  |
| `A:getattr_const|B:same_function|C:method_removed` | flag | xfail | dynamic getattr client construction is unresolved |
| `A:getattr_const|B:across_files|C:method_removed` | flag | xfail-pass | dynamic getattr client construction is unresolved |
| `A:context_manager|B:same_function|C:method_removed` | flag | pass |  |
| `A:context_manager|B:across_files|C:method_removed` | flag | pass |  |
| `A:async_await|B:same_function|C:method_removed` | flag | xfail-pass | async client flows are not fully modeled |
| `A:async_await|B:across_files|C:method_removed` | flag | xfail-pass | async client flows are not fully modeled |
| `A:direct|B:same_function|C:renamed` | flag | pass |  |
| `A:direct|B:same_function|C:required_param_added` | flag | xfail | missing required-arg calls are not yet flagged as breaks |
| `A:direct|B:same_function|C:param_removed_kw` | flag | pass |  |
| `A:direct|B:same_function|C:return_dict_to_object` | flag | xfail | return-shape / kind changes need typed response tracking |
| `A:direct|B:same_function|C:kind_function_to_class` | flag | xfail | return-shape / kind changes need typed response tracking |
| `A:direct|B:same_function|C:attr_removed_on_return` | flag | xfail | return-shape / kind changes need typed response tracking |
| `A:direct|B:same_function|C:default_changed` | noflag | pass |  |
| `A:direct|B:wrapper|C:method_removed` | flag | pass |  |
| `A:direct|B:method_chain|C:method_removed` | flag | pass |  |
| `A:direct|B:reassignment|C:method_removed` | flag | pass |  |
| `A:direct|B:closure|C:method_removed` | flag | xfail | closure/lambda captured clients are unresolved |
| `A:direct|B:lambda|C:method_removed` | flag | xfail-pass | closure/lambda captured clients are unresolved |

## Failing / xfail categories

- (4) container values are not tracked to call sites
- (3) return-shape / kind changes need typed response tracking
- (2) dynamic getattr client construction is unresolved
- (2) async client flows are not fully modeled
- (2) closure/lambda captured clients are unresolved
- (1) class-body Client() assignment not followed on instance
- (1) settings/config class attributes are not tracked
- (1) decorator-injected parameters often stay untyped
- (1) registry dict lookup of a class is unresolved
- (1) missing required-arg calls are not yet flagged as breaks

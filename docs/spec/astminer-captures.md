# 抽取查询捕获命名规范

本规范描述源码抽取包下全部查询文本的捕获命名约定。
词表以查询注册表中的集合为准，校验以配置检查中的命名校验为准，
两者不一致时以代码为准。本文只做整理，不增设规则。

## 1. 格式

全部捕获名由四域前缀开头，按点分段，最多四段：

- `entity.<类目>[.signature.<签名属性> | .<角色>...]`，实体定义
- `call.<类目>[.<角色>...]`，调用关系
- `dependency.<类目>[.<角色>...]`，依赖关系
- `comment.line`、`comment.doc`，注释，二者之外无其他注释捕获

`signature` 只出现在第三段，表示签名子通道；
签名子捕获嵌入实体捕获内部，不独立成匹配。
四段形态的中缀只允许三种：`signature`、`call`、`type`。
其中 `call` 实际只用于装饰器调用变体，`type` 只用于实现块归属，
其余四段一律不合法。

标记捕获只有存在性语义，不取文本：
`entity.package.file_scoped`、`dependency.import.wildcard`、
`dependency.use.wildcard`。

## 2. 实体域

### 2.1 类目

共二十二个：annotation、arrow、class、constructor、decorator、enum、
enum_constant、field、function、generator、impl、interface、macro、
method、module、object_method、package、record、struct、type、union、
variable。其中 `impl` 由引擎特判处理归属，不进入种类映射；
其余二十一个必须在种类映射表中有对应种类，否则校验报错。
`lambda` 在词表中保留，当前无查询使用。

### 2.2 主捕获覆盖

- `entity.annotation`：java
- `entity.arrow`：javascript、typescript
- `entity.class`：csharp、groovy、java、javascript、kotlin、python、typescript
- `entity.constructor`：csharp、java、kotlin
- `entity.decorator`：csharp、python
- `entity.enum`：csharp、java、rust、typescript
- `entity.enum_constant`：csharp、groovy、java、kotlin、rust、typescript
- `entity.field`：csharp、go、groovy、java、kotlin、python、rust、typescript
- `entity.function`：csharp、go、groovy、javascript、kotlin、python、rust、typescript
- `entity.generator`：javascript、typescript
- `entity.impl`：rust
- `entity.interface`：csharp、go、java、rust、typescript
- `entity.macro`：rust
- `entity.method`：csharp、go、groovy、java、javascript、python、typescript
- `entity.module`：rust
- `entity.object_method`：javascript、typescript
- `entity.package`：csharp、go、groovy、java、kotlin、typescript
- `entity.record`：csharp、java
- `entity.struct`：csharp、go、rust
- `entity.type`：go、kotlin、rust、typescript
- `entity.union`：rust
- `entity.variable`：csharp、go、groovy、java、javascript、rust、typescript

嵌套在类族下的函数由引擎按父链推导为方法，
查询侧无需重复建模，这也是部分语言无方法主捕获的原因。

### 2.3 角色

共十三个：name（实体名）、body（体起点，供签名切头与文档判定）、
params（参数表节点）、return_type（返回类型节点）、base（基类或接口表达式）、
type（类型标注）、value（初始值）、source（变量来源：被赋值或被迭代对象）、
receiver 与 owner（属主表达式，owner 优先、receiver 备选）、tag（附加元信息）、
call（装饰器调用变体中缀）、file_scoped（包级标记）。
角色集合允许超前于消费：已命名但暂未被规则消费的捕获予以保留，
不得因未使用而删除。

### 2.4 签名角色

共七个：name、type_params、params、return_type、base、source、arguments，
另加 type（变量与字段的类型纳入签名）。
合成时各部分原文保留、首尾去空，之间以单空格拼接。
合成与体前切片分支天然有界，不截断。
无签名别名时，有体取体前头切片，无体回退全文并按上限截断，
应有体而无体捕获返回空并计数。

### 2.5 特殊形态

`entity.decorator.call.name` 与 `entity.decorator.call.arguments`
描述装饰器为调用形的情形。`entity.impl.type.name` 描述实现块的归属类型，
供引擎做方法重挂。两者均为合法四段形态，不得简化或改名。

## 3. 调用域

### 3.1 类目

共八个：async、constructor、function、hof、macro、method、reference、
super。其中 super 只出现在多段形态中，无独立主捕获。

### 3.2 主捕获覆盖

- `call.async`：python
- `call.constructor`：csharp、java、javascript、typescript
- `call.function`：csharp、go、groovy、java、javascript、kotlin、python、rust、typescript
- `call.hof`：groovy、kotlin
- `call.macro`：rust
- `call.method`：csharp、go、java、javascript、kotlin、python、rust、typescript
- `call.reference`：java

### 3.3 角色与形态

共十四个角色：function（直接函数名）、object（接受者）、method（多段形态下的方法名中缀）、
name（名）、arguments（实参表）、from 与 to（链式来源与目标）、chained（链式中缀）、
class（超类名）、super（构造器委托标记）、static（静态调用中缀）、callback（高阶回调体）、
method_ref 与 lambda（回调种类）。

链式调用按层分解，内层有效、外层因含括号恒为未消解，
属设计行为。方法引用整体捕获后由首位建造者解析，
接受者取首个具名子节点，方法取末位标识符。
高阶调用与其回调同区间多模式归并，自带接受者，
规则按后缀优先级取舍，无需查询侧去重。

### 3.4 不进入调用的形态

标签元素不属于调用，查询文本中不得出现标签模式，
相关类目已从词表删除。构造器委托由引擎按节点类型跳过，
查询侧保留 `call.constructor.super` 标记形态以记录曾匹配，
引擎不产生调用实例。

## 4. 依赖域

### 4.1 类目与覆盖

共九个类目：embedding、export、extend、implement、import、module、
require、use、using。其中 module 暂无独立主捕获，
仅以多段形态出现。

- `dependency.embedding`：go
- `dependency.export`：javascript、typescript
- `dependency.extend`：csharp、java
- `dependency.implement`：csharp、java
- `dependency.import`：go、groovy、java、javascript、kotlin、python、typescript
- `dependency.require`：javascript、typescript
- `dependency.use`：rust
- `dependency.using`：csharp

### 4.2 角色

共二十四个：alias、callee、default、destruct、dynamic、exports、from、
future、import、items、list、module、name、named、namespace、opens、
path、provides、relative、requires、spec、static、uses、wildcard。
别名路径统一用 path，相对导入用 relative，通配用 wildcard 标记。
模块指令族（module.*）当前未被消费，予以保留供未来使用。

### 4.3 重导出

包内重导出不走依赖展开，由消解期的重导出闭包二次解析。
查询侧只需如实捕获导出语句的模块与条目，
不得在查询层做跨文件展开。

## 5. 注释域

仅 `comment.line` 与 `comment.doc`。注释只用于文档判定与分段，
不参与置信判定。文档字符串统一用 `comment.doc`，
各语言不得另起名字。

## 6. 校验与指纹

配置检查在编译全部四域查询之后执行命名校验：
逐名断言符合本规范，未知捕获直接报错并指明语言；
实体主捕获逐个核对种类映射表的处理能力，
无处理规则的根直接报错。校验失败即配置失败，
不存在静默忽略。

查询文本哈希与语法包版本联合构成指纹，
任一查询变更触发缓存失效。查询版本与词表版本同进退，
扩展词表即新命名，只能加法，不得改动已有名的含义。

## 7. 新增语言流程

新增语言按以下顺序登记：补齐四域查询文本，
注册表登记模块，语言配置登记后缀与语法包，
运行配置检查。校验通过前不得合入。
查询缺失某类目时允许空缺，不得 invent 不在词表中的名字；
确需新类目或新角色时，先扩展词表集合，
再在查询中使用，两者在同一改动内完成。

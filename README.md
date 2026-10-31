# Mandali

### Plans, subscriptions, and recurring billing in one place.

Mandali is a subscription management platform being developed to bring the full subscription lifecycle into one workspace: product and plan configuration, quotations, recurring invoices, payment tracking, and revenue reporting.

The project is based on the **Subscription Management System** hackathon problem statement and its Brightloop Software scenario, where spreadsheets, manual invoices, and missed renewals make running a subscription business difficult.

> **Project status:** Planning stage. This README documents the intended product and requirements. Application code, technology choices, and setup instructions have not been added yet. Features below describe planned capabilities.

## Why Mandali?

Subscription businesses need a reliable way to answer everyday questions: Which subscriptions renew today? Has an invoice been paid? When does a discount expire? How much revenue is still outstanding?

Mandali aims to replace disconnected spreadsheets and manual billing work with a shared system for administrators, internal staff, and customers.

## Planned capabilities

| Area | Scope |
| --- | --- |
| Accounts and access | Customer registration, email-based password resets, and role-based access for administrators, internal staff, and customers. |
| Product catalog | Products with names, types, sales prices, cost prices, notes, and variants that can adjust pricing. |
| Subscription plans | Daily, weekly, monthly, and yearly billing; plan pricing; minimum quantities; availability dates; and configurable closure, pause, and renewal policies. |
| Quotations | Reusable templates with a plan, product lines, and a defined validity period. |
| Subscription lifecycle | Unique subscription numbers, customer and plan details, dates, payment terms, itemized lines, and visible lifecycle states. |
| Recurring invoicing | Invoices generated from due subscriptions with customer details, product lines, taxes, discounts, and totals. |
| Taxes and discounts | Admin-configured tax rules and fixed or percentage discounts with eligibility rules, validity dates, and usage limits. |
| Payment tracking | Payments recorded against invoices with a method, amount, date, and note; visibility into paid and outstanding invoices. |
| Customer portal | Customers can view and manage their own subscriptions and access their own invoices. |
| Reporting | Filterable summaries of active subscriptions, revenue, payments, outstanding balances, and overdue invoices. |

## Who uses it?

| Role | Intended access |
| --- | --- |
| Administrator | Controls the system, configures products, plans, taxes, and discounts, and is the only role allowed to add internal staff. |
| Internal staff | Handles day-to-day customer, quotation, subscription, invoice, and payment work within assigned permissions. |
| Customer | Accesses only their own subscriptions and invoices through the customer portal. |

## Core workflows

### From quotation to subscription

1. Configure products, variants, and a billing plan.
2. Prepare an offer using a quotation template with the appropriate plan and product lines.
3. Assign the customer, subscription number, start and expiry dates, and payment terms.
4. Confirm the subscription and track its lifecycle:

```text
Draft -> Quotation -> Confirmed -> Active -> Closed
```

Each subscription line records its product, quantity, unit price, taxes, and amount. Early closure, pausing, renewal, and automatic closure depend on the plan's configured policies.

### From billing to payment

1. Identify subscriptions due for billing.
2. Generate invoices using the subscription's customer details and product lines.
3. Calculate taxes and apply eligible discounts automatically.
4. Confirm, send, or print invoices; support cancellation where needed.
5. Record payments against the correct invoice and track outstanding balances.

The main invoice lifecycle is:

```text
Draft -> Confirmed -> Paid
```

Billing must prevent both duplicate invoices and missed billing periods.

### Discount rules

Only administrators can create discount rules. Planned rules support:

- Fixed-amount or percentage discounts.
- Minimum purchase amounts or quantities.
- Start and end dates.
- Limits on the number of uses.
- Application to selected products or entire subscriptions.

Eligible discounts should appear automatically on the next invoice and stop applying when they expire or reach their usage limit.

## Key requirements

- **Unique accounts:** Each email address belongs to only one account.
- **Password rules:** Passwords must contain more than eight characters, including an uppercase letter, a lowercase letter, and a special character.
- **Password recovery:** Reset links are sent to the registered email address.
- **Access boundaries:** Customers can access only their own subscriptions and invoices; staff access is limited by permissions.
- **Billing accuracy:** Invoice data, taxes, and discounts must be derived consistently from the applicable subscription and configuration.
- **Payment visibility:** Paid, outstanding, and overdue invoices must be easy to identify.
- **Performance target:** Pages should respond in under two seconds with thousands of subscriptions. This is a requirement, not a measured result.

## Development roadmap

The following checklist organizes the problem statement into implementation milestones. It does not represent completed work.

- [ ] Account registration, login, password recovery, and role-based access.
- [ ] Product catalog, variants, and subscription plan configuration.
- [ ] Quotation templates and subscription lifecycle management.
- [ ] Recurring invoice generation and tax calculation.
- [ ] Discount eligibility, expiry, and usage tracking.
- [ ] Payment recording and outstanding invoice tracking.
- [ ] Customer self-service portal.
- [ ] Reporting, filters, and summary dashboards.
- [ ] Billing correctness, access control, and performance validation.

## Getting started

Clone the repository:

```bash
git clone https://github.com/Roky777/Mandali.git
cd Mandali
```

There is no runnable application yet. Installation steps, environment configuration, database setup, and development commands will be documented once the implementation is added.

## Requirements source

This scope was derived from `Subscription_Management_System.pdf`, titled **Subscription Management System**, which describes the Brightloop Software billing scenario. The PDF was supplied separately and is not included in this initial repository commit.

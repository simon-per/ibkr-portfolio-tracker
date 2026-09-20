import * as React from "react"
import { cn } from "@/lib/utils"

const Card = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn(
      // Surface, not outline: the canvas is a step darker than the card, so a soft
      // border and no shadow is what reads as "a panel" on both themes.
      "rounded-xl border border-border/70 bg-card text-card-foreground",
      className
    )}
    {...props}
  />
))
Card.displayName = "Card"

const CardHeader = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    // `p-[var(--card-padding)]`, not `p-4 sm:p-6`: see the token's definition in
    // index.css for why a responsive base would break ~14 call-site overrides on
    // desktop. The bracket form matters too — tailwind-merge classifies it as padding
    // by the bracket, so a call site's `p-0` or `pt-6` replaces it rather than racing
    // it in CSS source order. A `theme.extend.spacing` key like `p-card` would not be
    // recognised as padding and both would survive.
    className={cn("flex flex-col space-y-1.5 p-[var(--card-padding)]", className)}
    {...props}
  />
))
CardHeader.displayName = "CardHeader"

const CardTitle = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLHeadingElement>
>(({ className, ...props }, ref) => (
  <h3
    ref={ref}
    // `text-[length:...]` rather than `text-[...]` so tailwind-merge classifies it as
    // font-size: sixteen KPI cards pass `text-sm` here (CardTitle is used as a small
    // label, not a heading) and must keep winning.
    className={cn(
      "text-[length:var(--card-title-size)] font-semibold leading-none tracking-tight",
      className
    )}
    {...props}
  />
))
CardTitle.displayName = "CardTitle"

const CardDescription = React.forwardRef<
  HTMLParagraphElement,
  React.HTMLAttributes<HTMLParagraphElement>
>(({ className, ...props }, ref) => (
  <p
    ref={ref}
    className={cn("text-sm text-muted-foreground", className)}
    {...props}
  />
))
CardDescription.displayName = "CardDescription"

const CardContent = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div ref={ref} className={cn("p-[var(--card-padding)] pt-0", className)} {...props} />
))
CardContent.displayName = "CardContent"

const CardFooter = React.forwardRef<
  HTMLDivElement,
  React.HTMLAttributes<HTMLDivElement>
>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    className={cn("flex items-center p-[var(--card-padding)] pt-0", className)}
    {...props}
  />
))
CardFooter.displayName = "CardFooter"

export { Card, CardHeader, CardFooter, CardTitle, CardDescription, CardContent }

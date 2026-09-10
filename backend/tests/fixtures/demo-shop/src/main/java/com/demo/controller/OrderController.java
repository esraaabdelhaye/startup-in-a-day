package com.demo.controller;

import com.demo.domain.Order;
import com.demo.service.OrderService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Storefront ordering endpoints.
 *
 * <p>The brace in this javadoc } must not confuse span detection.
 */
@RestController
@RequestMapping("/orders")
public class OrderController {

    @Autowired
    private OrderService orderService;

    @PostMapping("/checkout")
    public ResponseEntity<Order> createOrder(@RequestBody Order order) {
        validate(order);
        Order placed = orderService.checkout(order);
        return ResponseEntity.ok(placed);
    }

    @GetMapping({"/{id}", "/{id}/detail"})
    public ResponseEntity<Order> getOrder(
            @PathVariable Long id) {
        Order found = orderService.findOrder(id);
        return ResponseEntity.ok(found);
    }

    private void validate(Order order) {
        if (order == null) {
            throw new IllegalArgumentException("order required { not a block");
        }
    }
}
